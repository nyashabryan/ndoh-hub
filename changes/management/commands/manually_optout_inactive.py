import csv
import itertools
import os

from demands import HTTPServiceError
from django.core.management import BaseCommand, CommandError
from django.core.validators import URLValidator
from requests import exceptions
from structlog import get_logger


def mk_validator(validator_class):
    def validator_callback(input_str):
        validator = validator_class()
        validator(input_str)
        return input_str
    return validator_callback


class Command(BaseCommand):

    help = ("Make HTTP requests to a ndoh-hub instance to initiate opt-out "
            "Changes for MSISDNs read from a CSV file (expected format "
            "<id>;<msisdn>;<count>)")

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv', type=file,
            help='CSV file to parse for MSISDNs')
        parser.add_argument(
            '--hub-token', type=str,
            default=os.environ.get('HUB_TOKEN'),
            help=('The authorization token for the NDoH Hub'))
        parser.add_argument(
            '--hub-url',
            type=mk_validator(URLValidator),
            default=os.environ.get('HUB_URL'),
            help=('The URL for the NDoH Hub API'))
        parser.add_argument(
            '--identity-store-token', type=str,
            default=os.environ.get('IDENTITY_STORE_TOKEN'),
            help='The token for the ID Store')
        parser.add_argument(
            '--identity-store-url',
            type=mk_validator(URLValidator),
            default=os.environ.get('IDENTITY_STORE_URL'),
            help='The Identity Store API URL')
        parser.add_argument(
            '--start', type=int,
            help='The row number to start from')
        parser.add_argument(
            '--end', type=int,
            help='The row number to end on (non-inclusive). Requires --start')

    def handle(self, *args, **options):
        identity_store_token = options['identity_store_token']
        identity_store_url = options['identity_store_url']
        hub_token = options['hub_token']
        hub_url = options['hub_url']
        csv_file = options['csv']
        start = options['start']
        end = options['end']
        log = get_logger()

        if not csv_file:
            raise CommandError('--csv is a required parameter')

        if not hub_url:
            raise CommandError('--hub-url is a required parameter')

        if not hub_token:
            raise CommandError('--hub-token is a required parameter')

        if not identity_store_url:
            raise CommandError('--identity-store-url is a required parameter')

        if not identity_store_token:
            raise CommandError('--identity-store-token is a required '
                               'parameter')

        from seed_services_client import IdentityStoreApiClient, HubApiClient

        ids_client = IdentityStoreApiClient(identity_store_token,
                                            identity_store_url)
        hub_client = HubApiClient(hub_token, hub_url)

        csv_reader = csv.reader(csv_file, delimiter=';')

        if start and end:
            rows = itertools.islice(csv_reader, start, end)
            row_count = start
        elif start and not end:
            rows = itertools.islice(csv_reader, start)
            row_count = start
        elif end and not start:
            raise CommandError('--start is a required parameter when '
                               'specifying --end')
        else:
            rows = csv_reader
            row_count = 1
            # skip the header row
            next(csv_reader)

        for idx, row in enumerate(rows, start=row_count):
            log = log.bind(row=idx)
            msisdn = '+{0}'.format(row[1])
            log = log.bind(msisdn=msisdn)
            result = ids_client.get_identity_by_address('msisdn', msisdn)
            if not result or 'count' not in result or result['count'] == 0:
                log.error('Could not load identity for msisdn.')
                continue

            if result['count'] > 1:
                msg = 'Warning: Found {0} identities'
                msg = msg.format(result['count'])
                log.warn(msg)
            identity = result['results'][0]
            # Use this logger only for this iteration of the loop
            id_log = log.bind(identity=identity['id'])
            change = {
                'registrant_id': identity['id'],
                'action': 'momconnect_nonloss_optout',
                'data': {
                    'reason': 'sms_failure'
                }
            }
            try:
                result = hub_client.create_change(change)
            except exceptions.ConnectionError as exc:
                id_log.error('Connection error to Hub API: {}'
                             .format(exc.message))
                break
            except HTTPServiceError as exc:
                id_log.error('Invalid Hub API response', url=exc.response.url,
                             status_code=exc.response.status_code)
                break

            if result:
                id_log.info('Successfully submitted changed.')
            else:
                id_log.error('Change failed', response=result)
