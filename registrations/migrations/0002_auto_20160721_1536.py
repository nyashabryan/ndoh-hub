# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2016-07-21 15:36
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registrations', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='registration',
            name='stage',
            field=models.CharField(choices=[
                ('momconnect_prebirth', 'MomConnect pregnancy registration'),
                ('momconnect_postbirth', 'MomConnect baby registration'),
                ('nurseconnect', 'Nurseconnect registration'),
                ('pmtct_prebirth', 'PMTCT pregnancy registration'),
                ('pmtct_postbirth', 'PMTCT baby registration'),
                ('loss_general', 'Loss general registration')
            ], max_length=30),
        ),
    ]
