# Generated by Django 4.2rc1 on 2023-11-16 10:45

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("django_comments_xtd", "0009_xtdcomment_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="xtdcomment",
            name="is_edited",
            field=models.BooleanField(default=False, verbose_name="是否被编辑"),
        ),
        migrations.AddField(
            model_name="xtdcomment",
            name="pinned_at",
            field=models.DateTimeField(
                blank=True, db_index=True, null=True, verbose_name="置顶时间"
            ),
        ),
    ]
