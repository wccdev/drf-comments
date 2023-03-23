from django.db import models


class CommentTypeChoices(models.TextChoices):
    """评论类型"""

    TYPE_NORMAL = "normal", "普通评论"
    TYPE_REMIND = "remind", "催一催"
