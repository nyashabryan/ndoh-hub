from celery import chain
from uuid import UUID
from django.core.management import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from changes.tasks import (
    restore_personally_identifiable_fields,
    remove_personally_identifiable_fields)


class Command(BaseCommand):

    help = ("Submit babyloss to Jembi. This command is useful if for "
            "some reason babyloss weren't submitted or failed to submit "
            "and need to be submitted again")

    def add_arguments(self, parser):
        parser.add_argument(
            '--since', type=parse_datetime,
            help='Filter for created_at since (required YYYY-MM-DD HH:MM:SS)')
        parser.add_argument(
            '--until', type=parse_datetime,
            help='Filter for created_at until (required YYYY-MM-DD HH:MM:SS)')
        parser.add_argument(
            '--source', type=int, default=None,
            help='The source to limit registrations to.')
        parser.add_argument(
            '--change', type=UUID, nargs='+', default=None,
            help=('UUIDs for babyloss` to fire manually. Use if finer '
                  'controls are needed than `since` and `until` date ranges.'))

    def handle(self, *args, **options):
        from changes.models import Change
        from changes.tasks import push_momconnect_babyloss_to_jembi
        since = options['since']
        until = options['until']
        source = options['source']
        change_uuids = options['change']

        if not (all([since, until]) or change_uuids):
            raise CommandError(
                'At a minimum please specify --since and --until '
                'or use --change to specify one or more changes')

        changes = Change.objects.filter(validated=True)

        if since and until:
            changes = changes.filter(
                created_at__gte=since,
                created_at__lte=until)

        if source is not None:
            changes = changes.filter(source__pk=source)

        if change_uuids is not None:
            changes = changes.filter(pk__in=change_uuids)

        self.stdout.write(
            'Submitting %s changes.' % (changes.count(),))
        for change in changes.filter(action__in=(
                'pmtct_loss_switch', 'momconnect_loss_switch')):
            restore_personally_identifiable_fields(change)
            change.save()
            push_task = push_momconnect_babyloss_to_jembi.si(str(change.pk))
            remove_info_task = remove_personally_identifiable_fields.si(
                str(change.pk))
            chain(push_task, remove_info_task).delay()
            self.stdout.write(str(change.pk))
        self.stdout.write('Done.')
