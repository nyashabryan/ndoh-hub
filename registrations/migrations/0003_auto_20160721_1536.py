# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2016-07-21 15:36
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('registrations', '0002_auto_20160721_1536'),
    ]

    operations = [
        migrations.RenameField(
            model_name='registration',
            old_name='stage',
            new_name='reg_type',
        ),
    ]
