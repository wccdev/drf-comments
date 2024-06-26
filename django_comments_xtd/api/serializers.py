import re

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.shortcuts import get_current_site
from django.utils import formats, timezone
from django.utils.html import escape
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _, activate, get_language

from django_comments import get_form
from django_comments.forms import CommentSecurityForm
from django_comments.models import CommentFlag
from django_comments.signals import comment_will_be_posted, comment_was_posted
from rest_framework import exceptions, serializers

from django_comments_xtd import get_model, signed, views
from django_comments_xtd.choices import CommentTypeChoices
from django_comments_xtd.conf import settings
from django_comments_xtd.models import (TmpXtdComment, XtdComment,
                                        LIKEDIT_FLAG, DISLIKEDIT_FLAG,
                                        max_thread_level_for_content_type)
from django_comments_xtd.signals import (should_request_be_authorized,
                                         confirmation_received, comment_was_updated, comment_was_removed)
from django_comments_xtd.utils import get_app_model_options, date_format

COMMENT_MAX_LENGTH = getattr(settings, 'COMMENT_MAX_LENGTH', None)

pattern = re.compile(r"^(<p>\s*?</p>)+|(<p>\s*?</p>)+$")


def handle_comment(s: str) -> str:
    if s:
        return pattern.sub("", s).strip()
    return s


class WriteCommentSerializer(serializers.Serializer):
    content_type = serializers.CharField()
    object_pk = serializers.CharField()
    timestamp = serializers.CharField(required=False)
    security_hash = serializers.CharField(required=False)
    honeypot = serializers.CharField(required=False, allow_blank=True)
    # name = serializers.CharField(allow_blank=True)
    # email = serializers.EmailField(allow_blank=True)
    url = serializers.URLField(required=False)
    comment = serializers.CharField(max_length=COMMENT_MAX_LENGTH, allow_blank=True)
    followup = serializers.BooleanField(required=False, default=False)
    reply_to = serializers.IntegerField(default=0)
    type = serializers.ChoiceField(required=False, choices=CommentTypeChoices.choices,
                                   default=CommentTypeChoices.TYPE_NORMAL)

    form = None

    def __init__(self, *args, **kwargs):
        self.request = kwargs['context']['request']
        super(WriteCommentSerializer, self).__init__(*args, **kwargs)

    def get_comment_name(self):
        """Set the comment name"""
        name = "unknown"
        if self.request.user.is_authenticated:
            if hasattr(self.request.user, "get_comment_display_name"):
                name = self.request.user.get_comment_display_name()
            elif hasattr(self.request.user, "get_username"):
                name = self.request.user.get_username()
            elif hasattr(self.request.user, "name"):
                name = self.request.user.name
            if name:
                return name
        return name

    def get_comment_email(self):
        """Set the comment email"""
        if self.request.user.is_authenticated:
            if hasattr(self.request.user, "get_email_field_name"):
                email = getattr(self.request.user, self.request.user.get_email_field_name(), None)
                if email:
                    return email
        return "unknown@wochacha.com"

    def validate_name(self, value):
        if value.strip():
            return value.strip()
        if self.request.user.is_authenticated:
            name = None
            if hasattr(self.request.user, "get_full_name"):
                name = self.request.user.get_full_name()
            elif hasattr(self.request.user, "get_username"):
                name = self.request.user.get_username()
            if name:
                return name
        raise serializers.ValidationError("This field is required")

    def validate_email(self, value):
        if value.strip():
            return value.strip()
        if self.request.user.is_authenticated:
            UserModel = apps.get_model(settings.AUTH_USER_MODEL)
            if hasattr(UserModel, "get_email_field_name"):
                email_field = UserModel.get_email_field_name()
                email = getattr(self.request.user, email_field, None)
                if email:
                    return email
        raise serializers.ValidationError("This field is required")

    def validate_reply_to(self, value):
        if value != 0:
            try:
                parent = get_model().objects.get(pk=value)
            except get_model().DoesNotExist:
                raise serializers.ValidationError(
                    "reply_to comment does not exist")
            else:
                max_thread_level = max_thread_level_for_content_type(
                    parent.content_type
                )
                if parent.level == max_thread_level:
                    raise serializers.ValidationError(
                        "Max thread level reached")
        return value

    def validate(self, data):
        data.update({
            "name": data.get("name", self.get_comment_name()),
            "email": data.get("email", self.get_comment_email()),
            "comment": handle_comment(data.get("comment")),
        })
        ctype = data.get("content_type")
        object_pk = data.get("object_pk")
        if ctype is None or object_pk is None:
            return serializers.ValidationError("Missing content_type or "
                                               "object_pk field.")
        try:
            model = apps.get_model(*ctype.split(".", 1))
            target = model._default_manager.get(pk=object_pk)
            whocan = get_app_model_options(content_type=ctype)['who_can_post']
        except (AttributeError, TypeError, LookupError, ValueError):
            raise serializers.ValidationError("Invalid content_type value: %r"
                                              % escape(ctype))
        except model.DoesNotExist:
            raise serializers.ValidationError(
                "No object matching content-type %r and object PK %r exists."
                % (escape(ctype), escape(object_pk)))
        except (serializers.ValidationError) as e:
            raise serializers.ValidationError(
                "Attempting to get content-type %r and object PK %r exists "
                "raised %s" % (escape(ctype), escape(object_pk),
                               e.__class__.__name__))
        else:
            if whocan == "users" and not self.request.user.is_authenticated:
                raise serializers.ValidationError("User not authenticated")

        # Signal that the request allows to be authorized.
        responses = should_request_be_authorized.send(
            sender=target.__class__,
            comment=target,
            request=self.request
        )

        for (receiver, response) in responses:
            if response is True:
                # A positive response indicates that the POST request
                # must be trusted. So inject the CommentSecurityForm values
                # to pass the form validation step.
                secform = CommentSecurityForm(target)
                data.update({
                    "honeypot": "",
                    "timestamp": secform['timestamp'].value(),
                    "security_hash": secform['security_hash'].value()
                })
                break
        self.form = get_form()(target, data=data)

        # Check security information.
        if self.form.security_errors():
            raise exceptions.PermissionDenied()
        if self.form.errors:
            raise serializers.ValidationError(self.form.errors)
        return data

    def save(self):
        # resp object is a dictionary. The code key indicates the possible
        # four states the comment can be in:
        #  * Comment created (http 201),
        #  * Confirmation sent by mail (http 204),
        #  * Comment in moderation (http 202),
        #  * Comment rejected (http 403),
        #  * Comment have bad data (http 400).
        site = get_current_site(self.request)
        resp = {
            'code': -1,
            'comment': self.form.get_comment_object(site_id=site.id)
        }
        resp['comment'].ip_address = self.request.META.get("REMOTE_ADDR", None)
        resp['comment'].type = self.validated_data.get("type")

        if self.request.user.is_authenticated:
            resp['comment'].user = self.request.user

        # Signal that the comment is about to be saved
        responses = comment_will_be_posted.send(sender=TmpXtdComment,
                                                comment=resp['comment'],
                                                request=self.request)
        for (receiver, response) in responses:
            if response is False:
                resp['code'] = 403  # Rejected.
                return resp

        # Replicate logic from django_comments_xtd.views.on_comment_was_posted.
        if (
                not settings.COMMENTS_XTD_CONFIRM_EMAIL or
                self.request.user.is_authenticated
        ):
            if views._get_comment_if_exists(resp['comment']) is None:
                new_comment = views._create_comment(resp['comment'])
                resp['comment'].xtd_comment = new_comment
                confirmation_received.send(sender=TmpXtdComment,
                                           comment=resp['comment'],
                                           request=self.request)
                comment_was_posted.send(sender=new_comment.__class__,
                                        comment=new_comment,
                                        request=self.request)
                if resp['comment'].is_public:
                    resp['code'] = 201
                    views.notify_comment_followers(new_comment)
                else:
                    resp['code'] = 202
        else:
            key = signed.dumps(resp['comment'], compress=True,
                               extra_key=settings.COMMENTS_XTD_SALT)
            views.send_email_confirmation_request(resp['comment'], key, site)
            resp['code'] = 204  # Confirmation sent by mail.

        return resp


class FlagSerializer(serializers.ModelSerializer):
    flag_choices = {'like': LIKEDIT_FLAG,
                    'dislike': DISLIKEDIT_FLAG,
                    'report': CommentFlag.SUGGEST_REMOVAL}

    class Meta:
        model = CommentFlag
        fields = ('comment', 'flag',)

    def validate(self, data):
        # Validate flag.
        if data['flag'] not in self.flag_choices:
            raise serializers.ValidationError("Invalid flag.")
        # Check commenting options on object being commented.
        option = ''
        if data['flag'] in ['like', 'dislike']:
            option = 'allow_feedback'
        elif data['flag'] == 'report':
            option = 'allow_flagging'
        comment = data['comment']
        ctype = ContentType.objects.get_for_model(comment.content_object)
        key = "%s.%s" % (ctype.app_label, ctype.model)
        if not get_app_model_options(content_type=key)[option]:
            raise serializers.ValidationError(
                "Comments posted to instances of '%s' are not explicitly "
                "allowed to receive '%s' flags. Check the "
                "COMMENTS_XTD_APP_MODEL_OPTIONS setting." % (key, data['flag'])
            )
        data['flag'] = self.flag_choices[data['flag']]
        return data


class ReadFlagField(serializers.RelatedField):
    def to_representation(self, value):
        if value.flag == CommentFlag.SUGGEST_REMOVAL:
            flag = "removal"
        elif value.flag == LIKEDIT_FLAG:
            flag = "like"
        elif value.flag == DISLIKEDIT_FLAG:
            flag = "dislike"
        else:
            raise Exception('Unexpected value for flag: %s' % value.flag)
        return {
            "flag": flag,
            "user": settings.COMMENTS_XTD_API_USER_REPR(value.user),
            "id": value.user.id
        }


class ReadCommentSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(max_length=50, read_only=True)
    user_url = serializers.CharField(read_only=True)
    # user_moderator = serializers.SerializerMethodField()
    user_avatar = serializers.SerializerMethodField()
    submit_date = serializers.SerializerMethodField()
    parent_id = serializers.IntegerField(default=0, read_only=True)
    level = serializers.IntegerField(read_only=True)
    is_removed = serializers.BooleanField(read_only=True)
    comment = serializers.SerializerMethodField()
    allow_reply = serializers.SerializerMethodField()
    permalink = serializers.SerializerMethodField()
    flags = ReadFlagField(many=True, read_only=True)
    type = serializers.CharField(label="评论类型", read_only=True)
    extra_data = serializers.SerializerMethodField(label="额外数据")

    class Meta:
        model = XtdComment
        fields = ('id', 'user_name', 'user_url',
                  'user_avatar', 'permalink', 'comment', 'submit_date',
                  'parent_id', 'level', 'is_removed', 'allow_reply', 'flags',
                  'type', 'extra_data', 'pinned_at', 'is_edited')

    def __init__(self, *args, **kwargs):
        self.request = kwargs['context']['request']
        super(ReadCommentSerializer, self).__init__(*args, **kwargs)

    def get_submit_date(self, obj):
        # activate(get_language())
        # if settings.USE_TZ:
        #     submit_date = timezone.localtime(obj.submit_date)
        # else:
        #     submit_date = obj.submit_date
        # return formats.date_format(submit_date, 'DATETIME_FORMAT',
        #                            use_l10n=True)
        return date_format(obj.submit_date)

    def get_comment(self, obj):
        if obj.is_removed:
            return _("This comment has been removed.")
        else:
            return obj.comment

    def get_user_moderator(self, obj):
        try:
            if obj.user and obj.user.has_perm('django_comments.can_moderate'):
                return True
            else:
                return False
        except Exception:
            return None

    def get_allow_reply(self, obj):
        return obj.allow_thread()

    def get_user_avatar(self, obj):
        return ""
        # return import_string(settings.COMMENTS_XTD_API_GET_USER_AVATAR)(obj)

    def get_permalink(self, obj):
        return ""
        # return obj.get_absolute_url()

    def get_extra_data(self, obj):
        if obj.user and hasattr(obj.user, 'get_extra_data'):
            return obj.user.get_extra_data()
        return None


class DestroyCommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = XtdComment
        fields = ('id',)


class UpdateCommentSerializer(serializers.ModelSerializer):
    comment = serializers.CharField(max_length=COMMENT_MAX_LENGTH, allow_blank=True)
    extra_data = serializers.SerializerMethodField(label="额外数据", read_only=True)

    def __init__(self, *args, **kwargs):
        self.request = kwargs['context']['request']
        super(UpdateCommentSerializer, self).__init__(*args, **kwargs)

    def get_extra_data(self, obj):
        if obj.user and hasattr(obj.user, 'get_extra_data'):
            return obj.user.get_extra_data()
        return None

    def update(self, instance, validated_data):
        original_comment = handle_comment(instance.comment)
        if self.request.user.pk != instance.user_id:
            raise Exception("You can only update your own comment")

        new_comment = handle_comment(validated_data.get('comment'))
        instance.comment = new_comment
        if new_comment:
            instance.is_edited = True
            instance.save()
            comment_was_updated.send(
                sender=instance.__class__,
                comment=instance,
                original_comment=original_comment,
                new_comment=new_comment,
            )
        else:
            # 当用户编辑后的评论为空时，删除该评论
            instance.is_edited = True
            instance.is_removed = True
            instance.save()
            comment_was_removed.send(sender=instance.__class__, comment=instance)

        return instance

    class Meta:
        model = XtdComment
        fields = ("comment", "type", "id", "submit_date", "pinned_at", "is_edited", "extra_data")
        read_only_fields = ("type", "submit_date", "pinned_at", "is_edited")
