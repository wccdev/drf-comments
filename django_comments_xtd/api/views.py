import six

from django.db.models import Prefetch
from django.contrib.contenttypes.models import ContentType
from django.utils.module_loading import import_string
from django.utils import timezone

from django_comments.models import CommentFlag
from django_comments.views.moderation import perform_flag
from rest_framework import generics, mixins, permissions, status, renderers
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.schemas.openapi import AutoSchema

from django_comments_xtd import views
from django_comments_xtd import get_model
from django_comments_xtd.api.serializers import DestroyCommentSerializer, UpdateCommentSerializer
from django_comments_xtd.conf import settings
from django_comments_xtd.api import serializers
from django_comments_xtd.models import (
    TmpXtdComment, LIKEDIT_FLAG, DISLIKEDIT_FLAG
)
from django_comments_xtd.signals import comment_was_removed, comment_was_pinned
from django_comments_xtd.utils import get_current_site_id, date_format

XtdComment = get_model()


class DefaultsMixin:
    # @property
    # def renderer_classes(self):
    #     if self.kwargs.get('override_drf_defaults', False):
    #         return renderers.JSONRenderer, renderers.BrowsableAPIRenderer
    #     return super().renderer_classes

    @property
    def pagination_class(self):
        if self.kwargs.get('override_drf_defaults', False):
            return None
        return super().pagination_class


class CommentCreate(DefaultsMixin, generics.CreateAPIView):
    """Create a comment."""
    serializer_class = serializers.WriteCommentSerializer

    resp_dict = {}

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            response = super(CommentCreate, self).post(request, *args, **kwargs)
        else:
            if 'non_field_errors' in serializer.errors:
                response_msg = serializer.errors['non_field_errors']
            else:
                response_msg = [k for k in six.iterkeys(serializer.errors)]
            return Response(response_msg, status=400)
        if self.resp_dict['code'] == 201:  # The comment has been created.
            extra_data = None
            if self.resp_dict['comment'].get('user') and hasattr(self.resp_dict['comment']['user'], 'get_extra_data'):
                extra_data = self.resp_dict['comment']['user'].get_extra_data()
            response.data.update({
                'id': self.resp_dict['comment']['xtd_comment'].id,
                'user_name': self.resp_dict['comment'].get('user_name', None),
                'submit_date': date_format(self.resp_dict['comment'].get('submit_date', None)),
                'extra_data': extra_data,
            })
            return response
        elif self.resp_dict['code'] in [202, 204, 403]:
            return Response({}, status=self.resp_dict['code'])

    def perform_create(self, serializer):
        self.resp_dict = serializer.save()


class CommentList(DefaultsMixin, generics.ListAPIView):
    """List all comments for a given ContentType and object ID."""
    serializer_class = serializers.ReadCommentSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self, **kwargs):
        content_type_arg = self.request.query_params.get('content_type', None)
        object_pk_arg = self.request.query_params.get('object_pk', None)
        try:
            app_label, model = content_type_arg.split(".")
            content_type = ContentType.objects.get_by_natural_key(app_label,
                                                                  model)
        except (AssertionError, ValueError, ContentType.DoesNotExist):
            qs = XtdComment.objects.none()
        else:
            flags_qs = CommentFlag.objects.filter(flag__in=[
                CommentFlag.SUGGEST_REMOVAL, LIKEDIT_FLAG, DISLIKEDIT_FLAG
            ]).prefetch_related('user')
            prefetch = Prefetch('flags', queryset=flags_qs)
            qs = XtdComment\
                .objects\
                .prefetch_related(prefetch)\
                .filter(
                    content_type=content_type,
                    object_pk=object_pk_arg,
                    site__pk=get_current_site_id(self.request),
                    is_public=True,
                    is_removed=False
                ).order_by('-submit_date')
        return qs


class CommentCount(DefaultsMixin, generics.GenericAPIView):
    """Get number of comments posted to a given ContentType and object ID."""
    serializer_class = serializers.ReadCommentSerializer
    permission_classes = (permissions.AllowAny,)

    def get_queryset(self):
        content_type_arg = self.kwargs.get('content_type', None)
        object_pk_arg = self.kwargs.get('object_pk', None)
        app_label, model = content_type_arg.split("-")
        content_type = ContentType.objects.get_by_natural_key(app_label, model)
        qs = XtdComment.objects.filter(content_type=content_type,
                                       object_pk=object_pk_arg,
                                       is_public=True)
        return qs

    def get(self, request, *args, **kwargs):
        return Response({'count': self.get_queryset().count()})


class ToggleFeedbackFlag(
        DefaultsMixin, generics.CreateAPIView, mixins.DestroyModelMixin):
    """Create and delete like/dislike flags."""

    serializer_class = serializers.FlagSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    # schema = AutoSchema(operation_id_base="Feedback")

    created = None

    def post(self, request, *args, **kwargs):
        response = super(ToggleFeedbackFlag, self).post(request, *args,
                                                        **kwargs)
        if self.created:
            return response
        else:
            return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_create(self, serializer):
        f = getattr(views, 'perform_%s' % self.request.data['flag'])
        self.created = f(self.request, serializer.validated_data['comment'])


class CreateReportFlag(DefaultsMixin, generics.CreateAPIView):
    """Create 'removal suggestion' flags."""

    serializer_class = serializers.FlagSerializer
    permission_classes = (permissions.IsAuthenticatedOrReadOnly,)

    # schema = AutoSchema(operation_id_base="ReportFlag")

    def post(self, request, *args, **kwargs):
        return super(CreateReportFlag, self).post(request, *args, **kwargs)

    def perform_create(self, serializer):
        perform_flag(self.request, serializer.validated_data['comment'])


@api_view(["POST"])
def preview_user_avatar(request, *args, **kwargs):
    """Fetch the image associated with the user previewing the comment."""
    temp_comment = TmpXtdComment({
        'user': None,
        'user_email': request.data['email']
    })
    if request.user.is_authenticated:
        temp_comment['user'] = request.user
    get_user_avatar = import_string(settings.COMMENTS_XTD_API_GET_USER_AVATAR)
    return Response({'url': get_user_avatar(temp_comment)})


class CommentDestroy(DefaultsMixin, generics.DestroyAPIView):
    queryset = XtdComment.objects.all()
    serializer_class = DestroyCommentSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.user != request.user:
            raise Exception("不允许删除他人评论")
        self.perform_destroy(instance)
        comment_was_removed.send(sender=instance.__class__, comment=instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        instance.is_removed = True
        instance.save()


class CommentPin(DefaultsMixin, generics.UpdateAPIView):
    queryset = XtdComment.objects.all()
    serializer_class = DestroyCommentSerializer

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        if instance.pinned_at:
            instance.pinned_at = None
        else:
            instance.pinned_at = timezone.now()
        instance.save()
        comment_was_pinned.send(sender=instance.__class__, comment=instance)

        return Response(status=status.HTTP_200_OK)


class CommentUpdate(DefaultsMixin, generics.UpdateAPIView):
    """更新评论"""
    queryset = XtdComment.objects.all()
    serializer_class = UpdateCommentSerializer

