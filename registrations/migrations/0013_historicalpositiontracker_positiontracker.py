# -*- coding: utf-8 -*-
# Generated by Django 1.11.10 on 2018-05-03 09:33
from __future__ import unicode_literals

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('registrations', '0012_registration_external_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='HistoricalPositionTracker',
            fields=[
                ('label', models.CharField(db_index=True, help_text='The unique label to identify the tracker', max_length=100)),
                ('position', models.IntegerField(default=1, help_text='The current position of the tracker')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField()),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical position tracker',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': 'history_date',
            },
        ),
        migrations.CreateModel(
            name='PositionTracker',
            fields=[
                ('label', models.CharField(help_text='The unique label to identify the tracker', max_length=100, primary_key=True, serialize=False)),
                ('position', models.IntegerField(default=1, help_text='The current position of the tracker')),
            ],
            options={
                'permissions': (('increment_position_positiontracker', 'Can increment the position'),),
            },
        ),
    ]