# Generated by Django 2.2.12 on 2022-03-29 07:27

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0269_interface_idx_include_nodeconfig"),
    ]

    operations = [
        migrations.AlterField(
            model_name="filesystem",
            name="uuid",
            field=models.TextField(default=uuid.uuid4),
        ),
        migrations.AlterField(
            model_name="filesystemgroup",
            name="uuid",
            field=models.TextField(default=uuid.uuid4),
        ),
        migrations.AlterField(
            model_name="partition",
            name="uuid",
            field=models.TextField(blank=True, default=uuid.uuid4, null=True),
        ),
        migrations.AlterField(
            model_name="virtualblockdevice",
            name="uuid",
            field=models.TextField(default=uuid.uuid4),
        ),
    ]
