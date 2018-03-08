import uuid

from django.contrib.postgres.fields import JSONField
from django.contrib.auth.models import User
from django.db import models

from django.utils.encoding import python_2_unicode_compatible


@python_2_unicode_compatible
class Source(models.Model):
    """ The source from which a registation originates.
        The User foreignkey is used to identify the source based on the
        user's api token.
    """
    AUTHORITY_CHOICES = (
        ('patient', "Patient"),
        ('advisor', "Trusted Advisor"),
        ('hw_partial', "Health Worker Partial"),
        ('hw_full', "Health Worker Full")
    )
    name = models.CharField(max_length=100, null=False, blank=False)
    user = models.ForeignKey(User, related_name='sources', null=False)
    authority = models.CharField(max_length=30, null=False, blank=False,
                                 choices=AUTHORITY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "%s" % self.name


@python_2_unicode_compatible
class Registration(models.Model):
    """ A registation submitted via Vumi or other sources.

    After a registation has been created, a task will fire that
    validates if the data provided is sufficient for the type
    of pregnancy.

    Args:
        reg_type (str): The type of registration
        data (json): Registration info in json format
        validated (bool): True if the registation has been
            validated after creation
        source (object): Auto-completed field based on the Api key
    """

    REG_TYPE_CHOICES = (
        ('momconnect_prebirth', "MomConnect pregnancy registration"),
        ('momconnect_postbirth', "MomConnect baby registration"),
        ('whatsapp_prebirth', "WhatsApp MomConnect pregnancy registration"),
        ('nurseconnect', "Nurseconnect registration"),
        ('whatsapp_nurseconnect', "WhatsApp Nurseconnect registration"),
        ('pmtct_prebirth', "PMTCT pregnancy registration"),
        ('whatsapp_pmtct_prebirth', "WhatsApp PMTCT pregnancy registration"),
        ('pmtct_postbirth', "PMTCT baby registration"),
        ('whatsapp_pmtct_postbirth', "WhatsApp PMTCT baby registration"),
        ('loss_general', "Loss general registration"),
        ('jembi_momconnect', "Jembi MomConnect registration. Set temporarily "
         "until we calculate which registration it should be")
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.CharField(
        unique=True, null=True, db_index=True, default=None, max_length=100,
        help_text="The ID of the registration in the external service that "
        "created the registration")
    reg_type = models.CharField(max_length=30, null=False, blank=False,
                                choices=REG_TYPE_CHOICES)
    registrant_id = models.CharField(max_length=36, null=True, blank=False)
    data = JSONField(null=True, blank=True)
    validated = models.BooleanField(default=False)
    source = models.ForeignKey(Source, related_name='registrations',
                               null=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, related_name='registrations_created',
                                   null=True)
    updated_by = models.ForeignKey(User, related_name='registrations_updated',
                                   null=True)
    user = property(lambda self: self.created_by)

    def __str__(self):
        return str(self.id)

    def get_subscription_requests(self):
        """
        Returns all possible subscriptions created for this registration.

        :returns: Django Queryset
        """
        return SubscriptionRequest.objects.filter(
            identity=self.registrant_id)


@python_2_unicode_compatible
class SubscriptionRequest(models.Model):
    """ A data model that maps to the Stagebased Store
    Subscription model. Created after a successful Registration
    validation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.CharField(max_length=36, null=False, blank=False)
    messageset = models.IntegerField(null=False, blank=False)
    next_sequence_number = models.IntegerField(default=1, null=False,
                                               blank=False)
    lang = models.CharField(max_length=6, null=False, blank=False)
    schedule = models.IntegerField(default=1)
    metadata = JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def serialize_hook(self, hook):
        # optional, there are serialization defaults
        # we recommend always sending the Hook
        # metadata along for the ride as well
        return {
            'hook': hook.dict(),
            'data': {
                'id': str(self.id),
                'identity': self.identity,
                'messageset': self.messageset,
                'next_sequence_number': self.next_sequence_number,
                'lang': self.lang,
                'schedule': self.schedule,
                'metadata': self.metadata,
                'created_at': self.created_at.isoformat(),
                'updated_at': self.updated_at.isoformat()
            }
        }

    def __str__(self):
        return str(self.id)
