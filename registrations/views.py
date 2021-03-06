import datetime
import django_filters
import django_filters.rest_framework as filters
import json
import requests
import logging
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_hooks.models import Hook
from rest_framework import viewsets, mixins, generics, status
from rest_framework.decorators import detail_route
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import (
    IsAuthenticated, IsAdminUser, DjangoModelPermissions)
from rest_framework.views import APIView
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.utils.encoders import JSONEncoder

from seed_services_client.identity_store import IdentityStoreApiClient
from .models import Source, Registration, PositionTracker
from .serializers import (UserSerializer, GroupSerializer,
                          SourceSerializer, RegistrationSerializer,
                          HookSerializer, CreateUserSerializer,
                          JembiHelpdeskOutgoingSerializer,
                          ThirdPartyRegistrationSerializer,
                          JembiAppRegistrationSerializer,
                          PositionTrackerSerializer)
from .tasks import validate_subscribe_jembi_app_registration
from ndoh_hub.utils import get_available_metrics


logger = logging.getLogger(__name__)


def transform_language_code(lang):
    return {
        'zu': 'zul_ZA',
        'xh': 'xho_ZA',
        'af': 'afr_ZA',
        'en': 'eng_ZA',
        'nso': 'nso_ZA',
        'tn': 'tsn_ZA',
        'st': 'sot_ZA',
        'ts': 'tso_ZA',
        'ss': 'ssw_ZA',
        've': 'ven_ZA',
        'nr': 'nbl_ZA'
    }[lang]


def CursorPaginationFactory(field):
    """
    Returns a CursorPagination class with the field specified by field
    """
    class CustomCursorPagination(CursorPagination):
        ordering = field

    name = '{}CursorPagination'.format(field.capitalize())
    CustomCursorPagination.__name__ = name
    CustomCursorPagination.__qualname__ = name

    return CustomCursorPagination


class IdCursorPagination(CursorPagination):
    ordering = "id"


class CreatedAtCursorPagination(CursorPagination):
    ordering = "-created_at"


class HookViewSet(viewsets.ModelViewSet):
    """
    Retrieve, create, update or destroy webhooks.
    """
    permission_classes = (IsAuthenticated,)
    queryset = Hook.objects.all()
    serializer_class = HookSerializer
    pagination_class = IdCursorPagination

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class UserViewSet(viewsets.ReadOnlyModelViewSet):

    """
    API endpoint that allows users to be viewed or edited.
    """
    permission_classes = (IsAuthenticated,)
    queryset = User.objects.all()
    serializer_class = UserSerializer
    pagination_class = IdCursorPagination


class UserView(APIView):
    """ API endpoint that allows users creation and returns their token.
    Only admin users can do this to avoid permissions escalation.
    """
    permission_classes = (IsAdminUser,)

    def post(self, request):
        '''Create a user and token, given an email. If user exists just
        provide the token.'''
        serializer = CreateUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data.get('email')
        try:
            user = User.objects.get(username=email)
        except User.DoesNotExist:
            user = User.objects.create_user(email, email=email)
        token, created = Token.objects.get_or_create(user=user)

        return Response(
            status=status.HTTP_201_CREATED, data={'token': token.key})


class GroupViewSet(viewsets.ReadOnlyModelViewSet):

    """
    API endpoint that allows groups to be viewed or edited.
    """
    permission_classes = (IsAuthenticated,)
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    pagination_class = IdCursorPagination


class SourceViewSet(viewsets.ModelViewSet):

    """
    API endpoint that allows sources to be viewed or edited.
    """
    permission_classes = (IsAdminUser,)
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    pagination_class = IdCursorPagination


class RegistrationPost(mixins.CreateModelMixin, generics.GenericAPIView):
    permission_classes = (IsAuthenticated,)
    queryset = Registration.objects.all()
    serializer_class = RegistrationSerializer

    def post(self, request, *args, **kwargs):
        # load the users sources - posting users should only have one source
        source = Source.objects.get(user=self.request.user)
        request.data["source"] = source.id
        return self.create(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user,
                        updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class RegistrationFilter(filters.FilterSet):
    """Filter for registrations created, using ISO 8601 formatted dates"""
    created_before = django_filters.IsoDateTimeFilter(name="created_at",
                                                      lookup_expr="lte")
    created_after = django_filters.IsoDateTimeFilter(name="created_at",
                                                     lookup_expr="gte")

    class Meta:
        model = Registration
        ('reg_type', 'registrant_id', 'validated', 'source', 'created_at')
        fields = ['reg_type', 'registrant_id', 'validated', 'source',
                  'created_before', 'created_after']


class RegistrationGetViewSet(viewsets.ReadOnlyModelViewSet):
    """ API endpoint that allows Registrations to be viewed.
    """
    permission_classes = (IsAuthenticated,)
    queryset = Registration.objects.all()
    serializer_class = RegistrationSerializer
    filter_class = RegistrationFilter
    pagination_class = CreatedAtCursorPagination


class JembiHelpdeskOutgoingView(APIView):
    """ API endpoint that allows the helpdesk to post messages to Jembi
    """
    permission_classes = (IsAuthenticated,)
    UNCLASSIFIED_MESSAGES_DEFAULT_LABEL = 'Unclassified'

    def build_jembi_helpdesk_json(self, validated_data):

        def jembi_format_date(date):
            return date.strftime("%Y%m%d%H%M%S")

        def get_software_type(channel_id):
            """ Returns the swt value based on the type of the Junebug channel.
                Defaults to sms type
            """
            if channel_id == "":
                return 2
            result = requests.get(
                '%s/jb/channels/%s' % (settings.JUNEBUG_BASE_URL, channel_id),
                headers={'Content-Type': 'application/json'},
                auth=(settings.JUNEBUG_USERNAME, settings.JUNEBUG_PASSWORD))
            result.raise_for_status()
            channel_config = result.json()

            if channel_config['result'].get('type', None) == \
                    settings.WHATSAPP_CHANNEL_TYPE:
                return 4
            return 2

        registration = Registration.objects\
            .filter(registrant_id=validated_data.get('user_id'))\
            .order_by('-created_at')\
            .first()
        swt = get_software_type(validated_data.get('inbound_channel_id', ''))

        json_template = {
            "encdate": jembi_format_date(
                validated_data.get('inbound_created_on')),
            "repdate": jembi_format_date(
                validated_data.get('outbound_created_on')),
            "mha": 1,
            "swt": swt,  # 1 ussd, 2 sms, 4 whatsapp
            "cmsisdn": validated_data.get('to'),
            "dmsisdn": validated_data.get('to'),
            "faccode":
                registration.data.get('faccode') if registration else None,
            "data": {
                "question": validated_data.get('reply_to'),
                "answer": validated_data.get('content'),
            },
            "class":
                validated_data.get('label') or
                self.UNCLASSIFIED_MESSAGES_DEFAULT_LABEL,
            "type": 7,  # 7 helpdesk
            "op": str(validated_data.get('helpdesk_operator_id')),
        }
        return json_template

    def post(self, request):
        if not (settings.JEMBI_BASE_URL and settings.JEMBI_USERNAME and
                settings.JEMBI_PASSWORD):
            return Response(
                'Jembi integration is not configured properly.',
                status.HTTP_503_SERVICE_UNAVAILABLE)

        serializer = JembiHelpdeskOutgoingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        post_data = self.build_jembi_helpdesk_json(serializer.validated_data)
        try:

            source = Source.objects.get(user=self.request.user.id)

            endpoint = 'helpdesk'
            if source.name == 'NURSE Helpdesk App':
                endpoint = 'nc/helpdesk'
                post_data['type'] = 12  # NC Helpdesk

            result = requests.post(
                urljoin(settings.JEMBI_BASE_URL, endpoint),
                headers={'Content-Type': 'application/json'},
                data=json.dumps(post_data),
                auth=(settings.JEMBI_USERNAME, settings.JEMBI_PASSWORD),
                verify=False)
            result.raise_for_status()
        except (requests.exceptions.HTTPError,) as e:
            if e.response.status_code == 400:
                logger.warning("400 Error when posting to Jembi.\n"
                               "Response: %s\nPayload:%s" %
                               (e.response.text, json.dumps(post_data)))
                return Response(
                    'Error when posting to Jembi. Body: %s Payload: %r' % (
                        e.response.content, post_data),
                    status=status.HTTP_400_BAD_REQUEST)
            else:
                raise e

        return Response(
            status=status.HTTP_200_OK)


class HealthcheckView(APIView):

    """ Healthcheck Interaction
        GET - returns service up - getting auth'd requires DB
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        status = 200
        resp = {
            "up": True,
            "result": {
                "database": "Accessible"
            }
        }
        return Response(resp, status=status)


class ThirdPartyRegistration(APIView):
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        is_client = IdentityStoreApiClient(
            api_url=settings.IDENTITY_STORE_URL,
            auth_token=settings.IDENTITY_STORE_TOKEN
        )
        serializer = ThirdPartyRegistrationSerializer(data=request.data)
        if serializer.is_valid():
            mom_msisdn = serializer.validated_data['mom_msisdn']
            hcw_msisdn = serializer.validated_data['hcw_msisdn']
            lang_code = serializer.validated_data['mom_lang']
            lang_code = transform_language_code(lang_code)
            authority = serializer.validated_data['authority']
            # load the users sources with authority mapping
            if authority == 'chw':
                source_auth = 'hw_partial'
            elif authority == 'clinic':
                source_auth = 'hw_full'
            else:
                source_auth = 'patient'
            source = Source.objects.get(user=self.request.user,
                                        authority=source_auth)
            if mom_msisdn != hcw_msisdn:
                # Get or create HCW Identity
                result = list(is_client.get_identity_by_address(
                        'msisdn', hcw_msisdn)['results'])
                if len(result) < 1:
                    identity = {
                        'details': {
                            'default_addr_type': 'msisdn',
                            'addresses': {
                                'msisdn': {
                                    hcw_msisdn: {'default': True}
                                }
                            }
                        }
                    }
                    hcw_identity = is_client.create_identity(identity)
                else:
                    hcw_identity = result[0]
            else:
                hcw_identity = None

            id_type = serializer.validated_data['mom_id_type']
            if hcw_identity is not None:
                operator = hcw_identity['id']
                device = hcw_msisdn
            else:
                operator = None
                device = mom_msisdn

            # auth: chw, clinic,
            # Get or create Mom Identity
            result = list(is_client.get_identity_by_address(
                    'msisdn', mom_msisdn)['results'])
            if len(result) < 1:
                identity = {
                    'details': {
                        'default_addr_type': 'msisdn',
                        'addresses': {
                            'msisdn': {
                                mom_msisdn: {'default': True}
                            }
                        },
                        'operator_id': operator,
                        'lang_code': lang_code,
                        'id_type': id_type,
                        'mom_dob': serializer.validated_data['mom_dob'],
                        'last_edd': serializer.validated_data['mom_edd'],
                        'faccode': serializer.validated_data['clinic_code'],
                        'consent': serializer.validated_data['consent'],
                        'last_mc_reg_on': authority,
                        'source': 'external',
                    },
                }
                if id_type == 'sa_id':
                    identity['details']['sa_id_no'] = (
                        serializer.validated_data['mom_id_no'])
                elif id_type == 'passport':
                    identity['details']['passport_origin'] = (
                        serializer.validated_data['mom_passport_origin'])
                    identity['details']['passport_no'] = (
                        serializer.validated_data['mom_id_no'])
                mom_identity = is_client.create_identity(identity)
            else:
                mom_identity = result[0]
                # Update Seed Identity record
                details = mom_identity['details']
                details['operator_id'] = operator
                details['lang_code'] = lang_code
                details['id_type'] = id_type
                details['mom_dob'] = serializer.validated_data['mom_dob']
                details['last_edd'] = serializer.validated_data['mom_edd']
                details['faccode'] = serializer.validated_data['clinic_code']
                details['consent'] = serializer.validated_data['consent']
                details['last_mc_reg_on'] = authority
                details['source'] = 'external'
                if id_type == 'sa_id':
                    details['sa_id_no'] = (
                        serializer.validated_data['mom_id_no'])
                elif id_type == 'passport':
                    details['passport_origin'] = (
                        serializer.validated_data['mom_passport_origin'])
                    details['passport_no'] = (
                        serializer.validated_data['mom_id_no'])
                mom_identity['details'] = details
                result = is_client.update_identity(mom_identity['id'],
                                                   data=mom_identity)
                # update_identity returns the object directly as JSON
                mom_identity = result

            # Create registration
            reg_data = {
                'operator_id': operator,
                'msisdn_registrant': mom_msisdn,
                'msisdn_device': device,
                'id_type': id_type,
                'language': lang_code,
                'mom_dob': serializer.validated_data['mom_dob'],
                'edd': serializer.validated_data['mom_edd'],
                'faccode': serializer.validated_data['clinic_code'],
                'consent': serializer.validated_data['consent'],
                'mha': serializer.validated_data.get('mha', 1),
                'swt': serializer.validated_data.get('swt', 1),
            }
            if 'encdate' in serializer.validated_data:
                reg_data['encdate'] = serializer.validated_data['encdate']
            if id_type == 'sa_id':
                reg_data['sa_id_no'] = (
                    serializer.validated_data['mom_id_no'])
            elif id_type == 'passport':
                reg_data['passport_origin'] = (
                    serializer.validated_data['mom_passport_origin'])
                reg_data['passport_no'] = (
                    serializer.validated_data['mom_id_no'])
            reg = Registration.objects.create(
                reg_type='momconnect_prebirth',
                registrant_id=mom_identity['id'],
                source=source,
                data=reg_data,
                created_by=self.request.user,
                updated_by=self.request.user
            )
            reg_serializer = RegistrationSerializer(instance=reg)
            return Response(
                reg_serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class JembiAppRegistration(APIView):
    """
    MomConnect prebirth registrations from the Jembi App
    """
    permission_classes = (IsAuthenticated,)
    serializer_class = JembiAppRegistrationSerializer

    @classmethod
    def create_registration(cls, user: User, data: dict) -> Registration:
        source = Source.objects.get(user=user)
        serializer = cls.serializer_class(data=data)
        serializer.is_valid(raise_exception=True)

        created = serializer.validated_data.pop('created')
        external_id = (
            serializer.validated_data.pop('external_id', None) or None)

        # We encode and decode from JSON to ensure dates are encoded properly
        data = json.loads(JSONEncoder().encode(serializer.validated_data))

        registration = Registration.objects.create(
            external_id=external_id, reg_type='jembi_momconnect',
            registrant_id=None, data=data, source=source,
            created_by=user)

        # Overwrite the created_at date with the one provided
        registration.created_at = created
        registration.save()

        validate_subscribe_jembi_app_registration.delay(
            registration_id=str(registration.pk))

        return registration

    def post(self, request: Request) -> Response:
        registration = self.create_registration(
            request.user, request.data)
        return Response(
            RegistrationSerializer(registration).data,
            status=status.HTTP_202_ACCEPTED)


class JembiAppRegistrationStatus(APIView):
    """
    Status of registrations
    """
    permission_classes = (IsAuthenticated,)

    @classmethod
    def get_registration(cls, user: User, reg_id: str) -> Registration:
        try:
            reg = Registration.objects.get(external_id=reg_id)
        except Registration.DoesNotExist:
            try:
                reg = get_object_or_404(Registration, id=reg_id)
            except ValidationError:
                raise Http404()

        if reg.created_by_id != user.id:
            raise PermissionDenied()
        return reg

    def get(self, request: Request, registration_id: str) -> Response:
        registration = self.get_registration(
            request.user, registration_id)
        return Response(registration.status, status=status.HTTP_200_OK)


class MetricsView(APIView):

    """ Metrics Interaction
        GET - returns list of all available metrics on the service
        POST - starts up the task that fires all the scheduled metrics
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        status = 200
        resp = {
            "metrics_available": get_available_metrics()
        }
        return Response(resp, status=status)

    def post(self, request, *args, **kwargs):
        status = 201
        # Uncomment line below if scheduled metrics are added
        # scheduled_metrics.apply_async()
        resp = {"scheduled_metrics_initiated": True}
        return Response(resp, status=status)


class IncrementPositionPermission(DjangoModelPermissions):
    """
    Allows POST requests if the user has the increment_position permission
    """
    perms_map = {
        'POST': ['%(app_label)s.increment_position_%(model_name)s'],
    }


class PositionTrackerViewset(
        mixins.CreateModelMixin, mixins.RetrieveModelMixin,
        mixins.ListModelMixin, viewsets.GenericViewSet):

    permission_classes = (DjangoModelPermissions,)
    queryset = PositionTracker.objects.all()
    serializer_class = PositionTrackerSerializer
    pagination_class = CursorPaginationFactory('label')

    @detail_route(
            methods=['post'],
            permission_classes=[IncrementPositionPermission])
    def increment_position(self, request, pk=None):
        """
        Increments the position on the specified position tracker. Only allows
        an update once every 12 hours to avoid retried HTTP requests
        incrementing the position more than once
        """
        position_tracker = self.get_object()

        time_difference = timezone.now() - position_tracker.modified_at
        if time_difference < datetime.timedelta(hours=12):
            return Response({
                "error": "The position may only be incremented once every 12 "
                         "hours",
                }, status=status.HTTP_400_BAD_REQUEST)

        position_tracker.position += 1
        position_tracker.save(update_fields=('position',))

        serializer = self.get_serializer(instance=position_tracker)
        return Response(serializer.data, status=status.HTTP_200_OK)
