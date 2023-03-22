from django.dispatch import receiver

from .signals import should_request_be_authorized


@receiver(should_request_be_authorized, dispatch_uid="check_authentication")
def check_authentication(sender, comment, request, **kwargs):
    if request.user and request.user.is_authenticated:
        return True
