import datetime
import json
import re
import requests
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from celery import chain
from celery.task import Task
from celery.utils.log import get_task_logger
from django.conf import settings
from requests.exceptions import HTTPError
from seed_services_client.identity_store import IdentityStoreApiClient
from seed_services_client.stage_based_messaging import StageBasedMessagingApiClient  # noqa
from six import iteritems

from ndoh_hub import utils
from ndoh_hub.celery import app
from registrations.models import Registration
from .models import Change
from registrations.models import SubscriptionRequest, Source
from registrations.tasks import add_personally_identifiable_fields


sbm_client = StageBasedMessagingApiClient(
    api_url=settings.STAGE_BASED_MESSAGING_URL,
    auth_token=settings.STAGE_BASED_MESSAGING_TOKEN
)

is_client = IdentityStoreApiClient(
    api_url=settings.IDENTITY_STORE_URL,
    auth_token=settings.IDENTITY_STORE_TOKEN
)


class ValidateImplement(Task):
    """ Task to apply a Change action.
    """
    name = "ndoh_hub.changes.tasks.validate_implement"
    log = get_task_logger(__name__)

    # Helpers
    def deactivate_all(self, change):
        """ Deactivates all subscriptions for an identity
        """
        self.log.info("Retrieving active subscriptions")
        active_subs = sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True}
        )["results"]

        self.log.info("Deactivating all active subscriptions")
        for active_sub in active_subs:
            sbm_client.update_subscription(
                active_sub["id"], {"active": False})

        self.log.info("All subscriptions deactivated")
        return True

    def deactivate_all_except_nurseconnect(self, change):
        """ Deactivates all subscriptions for an identity that are not to
        nurseconnect
        """
        self.log.info("Retrieving active subscriptions")
        active_subs = sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True}
        )["results"]

        self.log.info("Retrieving nurseconnect messagesets")
        messagesets = sbm_client.get_messagesets()["results"]
        nc_messageset_ids = [ms['id'] for ms in messagesets
                             if 'nurseconnect' in ms['short_name']]

        self.log.info("Deactivating active non-nurseconnect subscriptions")
        for active_sub in active_subs:
            if active_sub["messageset"] not in nc_messageset_ids:
                sbm_client.update_subscription(
                    active_sub["id"], {"active": False})

        self.log.info("Non-nurseconnect subscriptions deactivated")
        return True

    def deactivate_nurseconnect(self, change):
        """ Deactivates nurseconnect subscriptions only
        """
        self.log.info("Retrieving messagesets")
        messagesets = sbm_client.get_messagesets()["results"]
        nc_messagesets = [ms for ms in messagesets
                          if 'nurseconnect' in ms['short_name']]

        self.log.info("Retrieving active nurseconnect subscriptions")
        active_subs = []
        for messageset in nc_messagesets:
            active_subs.extend(sbm_client.get_subscriptions(
                {'identity': change.registrant_id, 'active': True,
                 'messageset': messageset["id"]}
            )["results"])

        self.log.info("Deactivating active nurseconnect subscriptions")
        for active_sub in active_subs:
            sbm_client.update_subscription(active_sub["id"], {"active": False})

    def deactivate_pmtct(self, change):
        """ Deactivates any pmtct subscriptions
        """
        self.log.info("Retrieving active subscriptions")
        active_subs = sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True}
        )["results"]

        self.log.info("Deactivating active pmtct subscriptions")
        for active_sub in active_subs:
            messageset = sbm_client.get_messageset(active_sub["messageset"])
            if "pmtct" in messageset["short_name"]:
                self.log.info("Deactivating messageset %s" % messageset["id"])
                sbm_client.update_subscription(
                    active_sub["id"], {"active": False})

    def loss_switch(self, change):
        self.log.info("Retrieving active subscriptions")
        active_subs = list(sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True}
        )["results"])

        if (len(active_subs) == 0):
            self.log.info("No active subscriptions - aborting")
            return False

        whatsapp = False
        messagesets = list(sbm_client.get_messagesets()['results'])
        for sub in active_subs:
            for ms in messagesets:
                if ms['id'] == sub['messageset']:
                    short_name = ms['short_name']
            if 'whatsapp' in short_name:
                whatsapp = True

        # TODO: Provide temporary bridging code while both systems are
        # being used. The purpose of this would be to accommodate making
        # changes to ndoh-hub that need to be deployed to production while
        # the old system is still in use in production while the new system
        # is in use in QA.
        # If the active subscriptions do not include a momconnect
        # subscription, it means the old system is still being used.

        else:
            self.deactivate_all_except_nurseconnect(change)

            self.log.info("Determining messageset shortname")
            set_name = 'loss_{}'.format(change.data['reason'])
            if whatsapp:
                set_name = 'whatsapp_{}'.format(set_name)
            short_name = utils.get_messageset_short_name(
                set_name, "patient", 0)

            self.log.info("Determining SBM details")
            msgset_id, msgset_schedule, next_sequence_number =\
                utils.get_messageset_schedule_sequence(
                    short_name, 0)

            self.log.info("Determining language")
            identity = is_client.get_identity(change.registrant_id)

            subscription = {
                "identity": change.registrant_id,
                "messageset": msgset_id,
                "next_sequence_number": next_sequence_number,
                "lang": identity["details"]["lang_code"],
                "schedule": msgset_schedule
            }

            self.log.info("Creating Loss SubscriptionRequest")
            SubscriptionRequest.objects.create(**subscription)
            self.log.info("Created Loss SubscriptionRequest")
            return True

    # Action implementation
    def baby_switch(self, change):
        """ This should be applied when a mother has her baby. Currently it
        only changes the pmtct subscription, but in the future it will also
        change her momconnect subscription.
        """
        self.log.info("Starting switch to baby")

        self.log.info("Retrieving active subscriptions")
        active_subs = sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True}
        )["results"]

        # Determine if the mother has an active pmtct subscription and
        # deactivate active subscriptions
        self.log.info("Evaluating active subscriptions")
        has_active_pmtct_prebirth_sub = False
        has_active_whatsapp_pmtct_prebirth_sub = False
        has_active_momconnect_prebirth_sub = False
        has_active_whatsapp_momconnect_prebirth_sub = False

        for active_sub in active_subs:
            self.log.info("Retrieving messageset")
            messageset = sbm_client.get_messageset(active_sub["messageset"])
            if "pmtct_prebirth" in messageset["short_name"]:
                if "whatsapp" in messageset["short_name"]:
                    has_active_whatsapp_pmtct_prebirth_sub = True
                has_active_pmtct_prebirth_sub = True
                lang = active_sub["lang"]
            if "momconnect_prebirth" in messageset["short_name"]:
                has_active_momconnect_prebirth_sub = True
                if "whatsapp" in messageset["short_name"]:
                    has_active_whatsapp_momconnect_prebirth_sub = True
                lang = active_sub["lang"]
            if "prebirth" in messageset["short_name"]:
                self.log.info("Deactivating subscription")
                sbm_client.update_subscription(
                    active_sub["id"], {"active": False})

        if has_active_momconnect_prebirth_sub:
            self.log.info("Starting postbirth momconnect subscriptionrequest")

            self.log.info("Determining messageset shortname")
            # . determine messageset shortname
            short_name = utils.get_messageset_short_name(
                "momconnect_postbirth", "hw_full", 0)
            if has_active_whatsapp_momconnect_prebirth_sub:
                short_name = 'whatsapp_{}'.format(short_name)

            # . determine sbm details
            self.log.info("Determining SBM details")
            msgset_id, msgset_schedule, next_sequence_number =\
                utils.get_messageset_schedule_sequence(short_name, 0)

            subscription = {
                "identity": change.registrant_id,
                "messageset": msgset_id,
                "next_sequence_number": next_sequence_number,
                "lang": lang,
                "schedule": msgset_schedule
            }
            self.log.info("Creating MomConnect postbirth SubscriptionRequest")
            SubscriptionRequest.objects.create(**subscription)
            self.log.info("Created MomConnect postbirth SubscriptionRequest")

        if has_active_pmtct_prebirth_sub:
            self.log.info("Starting postbirth pmtct subscriptionrequest")

            self.log.info("Determining messageset shortname")
            # . determine messageset shortname
            set_name = 'pmtct_postbirth'
            if has_active_whatsapp_pmtct_prebirth_sub:
                set_name = 'whatsapp_{}'.format(set_name)
            short_name = utils.get_messageset_short_name(
                set_name, "patient", 0)

            # . determine sbm details
            self.log.info("Determining SBM details")
            msgset_id, msgset_schedule, next_sequence_number =\
                utils.get_messageset_schedule_sequence(short_name, 0)

            subscription = {
                "identity": change.registrant_id,
                "messageset": msgset_id,
                "next_sequence_number": next_sequence_number,
                "lang": lang,
                "schedule": msgset_schedule
            }
            self.log.info("Creating PMTCT postbirth SubscriptionRequest")
            SubscriptionRequest.objects.create(**subscription)
            self.log.info("Created PMTCT postbirth SubscriptionRequest")

        self.log.info("Saving the date of birth to the identity")
        identity = is_client.get_identity(change.registrant_id)
        details = identity["details"]
        details["last_baby_dob"] = utils.get_today().strftime("%Y-%m-%d")
        is_client.update_identity(
            change.registrant_id, {"details": details})
        self.log.info("Saved the date of birth to the identity")

        return push_momconnect_babyswitch_to_jembi.si(str(change.pk))

    def pmtct_loss_switch(self, change):
        """ Deactivate any active momconnect & pmtct subscriptions, then
        subscribe them to loss messages.
        """
        self.log.info("Starting PMTCT switch to loss")
        switched = self.loss_switch(change)

        if switched is True:
            self.log.info("Completed PMTCT switch to loss")
            return push_momconnect_babyloss_to_jembi.si(str(change.pk))
        else:
            self.log.info("Aborted PMTCT switch to loss")

    def pmtct_loss_optout(self, change):
        """ This only deactivates non-nurseconnect subscriptions
        """
        self.log.info("Starting PMTCT loss optout")
        self.deactivate_all_except_nurseconnect(change)
        self.log.info("Completed PMTCT loss optout")
        self.log.info("Sending optout to Jembi")
        return push_pmtct_optout_to_jembi.si(str(change.pk))

    def pmtct_nonloss_optout(self, change):
        """ Identity optout only happens for SMS optout and is done
        in the JS app. SMS optout deactivates all subscriptions,
        whereas USSD optout deactivates only pmtct subscriptions
        """
        self.log.info("Starting PMTCT non-loss optout")

        if change.data["reason"] == 'unknown':  # SMS optout
            self.deactivate_all(change)
        else:
            self.deactivate_pmtct(change)

        self.log.info("Completed PMTCT non-loss optout")
        self.log.info("Sending optout to Jembi")
        return push_pmtct_optout_to_jembi.si(str(change.pk))

    def nurse_update_detail(self, change):
        """ This currently does nothing, but in a seperate issue this will
        handle sending the information update to Jembi
        """
        self.log.info("Starting nurseconnect detail update")
        self.log.info("Completed nurseconnect detail update")

    def nurse_change_msisdn(self, change):
        """ This currently does nothing, but in a seperate issue this will
        handle sending the information update to Jembi
        """
        self.log.info("Starting nurseconnect msisdn change")
        self.log.info("Completed nurseconnect msisdn change")

    def nurse_optout(self, change):
        """ The rest of the action required (opting out the identity on the
        identity store) is currently done via the ndoh-jsbox ussd_nurse
        app, we're only deactivating any NurseConnect subscriptions here.
        """
        self.log.info("Starting NurseConnect optout")
        self.deactivate_nurseconnect(change)
        self.log.info("Pushing optout to Jembi")
        return push_nurseconnect_optout_to_jembi.si(str(change.pk))

    def momconnect_loss_switch(self, change):
        """ Deactivate any active momconnect & pmtct subscriptions, then
        subscribe them to loss messages.
        """
        self.log.info("Starting MomConnect switch to loss")
        switched = self.loss_switch(change)

        if switched is True:
            self.log.info("Completed MomConnect switch to loss")
            return push_momconnect_babyloss_to_jembi.si(str(change.pk))
        else:
            self.log.info("Aborted MomConnect switch to loss")

    def momconnect_loss_optout(self, change):
        """ This only deactivates non-nurseconnect subscriptions
        """
        self.log.info("Starting MomConnect loss optout")
        self.deactivate_all_except_nurseconnect(change)
        self.log.info("Completed MomConnect loss optout")
        self.log.info("Sending optout to Jembi")
        return push_momconnect_optout_to_jembi.si(str(change.pk))

    def momconnect_nonloss_optout(self, change):
        """ Identity optout only happens for SMS optout and is done
        in the JS app. SMS optout deactivates all subscriptions,
        whereas USSD optout deactivates all except nurseconnect
        """
        self.log.info("Starting MomConnect non-loss optout")

        if (change.data["reason"] == 'unknown' or
                change.data["reason"] == 'sms_failure'):  # SMS optout
            self.deactivate_all(change)
        else:
            self.deactivate_all_except_nurseconnect(change)

        self.log.info("Completed MomConnect non-loss optout")
        self.log.info("Sending optout to Jembi")
        return push_momconnect_optout_to_jembi.si(str(change.pk))

    def momconnect_change_language(self, change):
        """
        Language change should change the language of the identity, as well
        as the language of that identity's subscriptions.
        """
        self.log.info("Starting MomConnect language change")

        language = change.data['language']

        active_subs = sbm_client.get_subscriptions(
            {'identity': change.registrant_id, 'active': True})
        for sub in active_subs['results']:
            self.log.info(
                "Getting messageset for subscription {}".format(sub['id']))
            messageset = sbm_client.get_messageset(sub['messageset'])
            if 'momconnect' not in messageset['short_name']:
                continue
            self.log.info(
                "Changing language for subscription {}".format(sub['id']))
            sbm_client.update_subscription(sub['id'], {
                'lang': language,
                })

        self.log.info("Fetching identity {}".format(change.registrant_id))
        identity = is_client.get_identity(change.registrant_id)

        self.log.info("Updating Change object")
        change.data['old_language'] = identity['details'].get('lang_code')
        change.save()

        self.log.info(
            "Changing language for identity {}".format(change.registrant_id))
        identity['details']['lang_code'] = language
        is_client.update_identity(identity['id'], {
            'details': identity['details']
            })

    def momconnect_change_msisdn(self, change):
        """
        MSISDN change should change the default msisdn of the identity, while
        storing information on the identity to allow us to see the history
        of the change.
        """
        self.log.info("Starting MomConnect MSISDN change")

        new_msisdn = change.data.pop('msisdn')

        self.log.info("Fetching identity")
        identity = is_client.get_identity(change.registrant_id)

        self.log.info("Updating identity msisdn")
        details = identity['details']
        if 'addresses' not in details:
            details['addresses'] = {}
        addresses = details['addresses']
        if 'msisdn' not in addresses:
            addresses['msisdn'] = {}
        msisdns = addresses['msisdn']

        if not any(details.get('default') for _, details in msisdns.items()):
            for address, addr_details in msisdns.items():
                utils.append_or_create(addr_details, 'changes_from', change.id)

        for address, addr_details in msisdns.items():
            if 'default' in addr_details and addr_details['default']:
                addr_details['default'] = False
                utils.append_or_create(addr_details, 'changes_from', change.id)

        if new_msisdn not in msisdns:
            msisdns[new_msisdn] = {
                "default": True,
            }
        else:
            msisdns[new_msisdn]['default'] = True
        utils.append_or_create(msisdns[new_msisdn], 'changes_to', change.id)

        is_client.update_identity(identity['id'], {
            'details': details,
            })

        self.log.info("Updating Change object")
        change.save()

    def momconnect_change_identification(self, change):
        """
        Identification change should change the identification information on
        the identity, while storing the historical information on the identity.
        """
        self.log.info("Starting MomConnect Identification change")

        self.log.info("Fetching Identity")
        identity = is_client.get_identity(change.registrant_id)
        details = identity['details']
        old_identification = {'change': change.id}
        for field in ('sa_id_no', 'passport_no', 'passport_origin'):
            if field in details:
                old_identification[field] = details.pop(field)
        utils.append_or_create(
            details, 'identification_history', old_identification)

        id_type = change.data.pop('id_type')
        if id_type == 'sa_id':
            details['sa_id_no'] = change.data.pop('sa_id_no')
        else:
            details['passport_no'] = change.data.pop('passport_no')
            details['passport_origin'] = change.data.pop('passport_origin')

        self.log.info("Updating Identity")
        is_client.update_identity(identity['id'], {'details': details})

        self.log.info("Updating Change")
        change.save()

    def admin_change_subscription(self, change):
        """
        Messaging change should disable current subscriptions and create a new
        one with new message set
        """
        if change.data.get("messageset"):
            subscription = sbm_client.get_subscription(
                change.data['subscription'])
            current_nsn = subscription["next_sequence_number"]

            # Deactivate subscription
            sbm_client.update_subscription(subscription['id'],
                                           {"active": False})

            new_messageset = next(sbm_client.get_messagesets(
                {"short_name": change.data['messageset']})["results"])

            # Make new subscription request object
            mother_sub = {
                "identity": change.registrant_id,
                "messageset": new_messageset['id'],
                "next_sequence_number": current_nsn,
                "lang": change.data.get("language", subscription["lang"]),
                "schedule": new_messageset['default_schedule']
            }
            SubscriptionRequest.objects.create(**mother_sub)

        elif change.data.get("language"):
            sbm_client.update_subscription(change.data['subscription'], {
                'lang': change.data["language"],
            })

    def switch_channel(self, change):
        """
        Switch all active subscriptions to the desired channel
        """
        messagesets = {
            ms['id']: ms['short_name'] for ms in
            sbm_client.get_messagesets()['results']
        }
        messagesets_rev = {v: k for k, v in messagesets.items()}
        params = {
            'identity': change.registrant_id,
            'active': True,
        }
        for sub in sbm_client.get_subscriptions(params)['results']:
            if not sub['active']:
                continue
            short_name = messagesets[sub['messageset']]
            if (
                    change.data['channel'] == 'whatsapp' and
                    'whatsapp' not in short_name):
                # Change any SMS subscriptions to WhatsApp
                sbm_client.update_subscription(sub['id'], {'active': False})
                messageset = messagesets_rev['whatsapp_' + short_name]
                SubscriptionRequest.objects.create(
                    identity=sub['identity'],
                    messageset=messageset,
                    next_sequence_number=sub['next_sequence_number'],
                    lang=sub['lang'],
                    schedule=sub['schedule'],
                )
            elif change.data['channel'] == 'sms' and 'whatsapp' in short_name:
                # Change any WhatsApp subscriptions to SMS
                sbm_client.update_subscription(sub['id'], {'active': False})
                messageset = messagesets_rev[
                    re.sub('^whatsapp_', '', short_name)]
                SubscriptionRequest.objects.create(
                    identity=sub['identity'],
                    messageset=messageset,
                    next_sequence_number=sub['next_sequence_number'],
                    lang=sub['lang'],
                    schedule=sub['schedule'],
                )

        change.data['old_channel'] = 'sms'
        if change.data['channel'] == 'sms':
            change.data['old_channel'] = 'whatsapp'

        change.save()

        return push_channel_switch_to_jembi.si(str(change.pk))

    # Validation checks
    def check_pmtct_loss_optout_reason(self, data_fields, change):
        loss_reasons = ["miscarriage", "stillbirth", "babyloss"]
        if "reason" not in data_fields:
            return ["Optout reason is missing"]
        elif change.data["reason"] not in loss_reasons:
            return ["Not a valid loss reason"]
        else:
            return []

    def check_pmtct_nonloss_optout_reason(self, data_fields, change):
        nonloss_reasons = ["not_hiv_pos", "not_useful", "other", "unknown"]
        if "reason" not in data_fields:
            return ["Optout reason is missing"]
        elif change.data["reason"] not in nonloss_reasons:
            return ["Not a valid nonloss reason"]
        else:
            return []

    def check_nurse_update_detail(self, data_fields, change):
        if len(data_fields) == 0:
            return ["No details to update"]

        elif "faccode" in data_fields:
            if len(data_fields) != 1:
                return ["Only one detail update can be submitted per Change"]
            elif not utils.is_valid_faccode(change.data["faccode"]):
                return ["Faccode invalid"]
            else:
                return []

        elif "sanc_no" in data_fields:
            if len(data_fields) != 1:
                return ["Only one detail update can be submitted per Change"]
            elif not utils.is_valid_sanc_no(change.data["sanc_no"]):
                return ["sanc_no invalid"]
            else:
                return []

        elif "persal_no" in data_fields:
            if len(data_fields) != 1:
                return ["Only one detail update can be submitted per Change"]
            elif not utils.is_valid_persal_no(change.data["persal_no"]):
                return ["persal_no invalid"]
            else:
                return []

        elif "id_type" in data_fields and not (
          change.data["id_type"] in ["passport", "sa_id"]):
            return ["ID type should be passport or sa_id"]

        elif "id_type" in data_fields and change.data["id_type"] == "sa_id":
            if len(data_fields) != 3 or set(data_fields) != set(
              ["id_type", "sa_id_no", "dob"]):
                return ["SA ID update requires fields id_type, sa_id_no, dob"]
            elif not utils.is_valid_date(change.data["dob"]):
                return ["Date of birth is invalid"]
            elif not utils.is_valid_sa_id_no(change.data["sa_id_no"]):
                return ["SA ID number is invalid"]
            else:
                return []

        elif "id_type" in data_fields and change.data["id_type"] == "passport":
            if len(data_fields) != 4 or set(data_fields) != set(
              ["id_type", "passport_no", "passport_origin", "dob"]):
                return ["Passport update requires fields id_type, passport_no,"
                        " passport_origin, dob"]
            elif not utils.is_valid_date(change.data["dob"]):
                return ["Date of birth is invalid"]
            elif not utils.is_valid_passport_no(change.data["passport_no"]):
                return ["Passport number is invalid"]
            elif not utils.is_valid_passport_origin(
              change.data["passport_origin"]):
                return ["Passport origin is invalid"]
            else:
                return []

        else:
            return ["Could not parse detail update request"]

    def check_nurse_change_msisdn(self, data_fields, change):
        if len(data_fields) != 3 or set(data_fields) != set(
          ["msisdn_old", "msisdn_new", "msisdn_device"]):
            return ["SA ID update requires fields msisdn_old, msisdn_new, "
                    "msisdn_device"]
        elif not utils.is_valid_msisdn(change.data["msisdn_old"]):
            return ["Invalid old msisdn"]
        elif not utils.is_valid_msisdn(change.data["msisdn_new"]):
            return ["Invalid old msisdn"]
        elif not (change.data["msisdn_device"] == change.data["msisdn_new"] or
                  change.data["msisdn_device"] == change.data["msisdn_old"]):
            return ["Device msisdn should be the same as new or old msisdn"]
        else:
            return []

    def check_nurse_optout(self, data_fields, change):
        valid_reasons = ["job_change", "number_owner_change", "not_useful",
                         "other", "unknown"]
        if "reason" not in data_fields:
            return ["Optout reason is missing"]
        elif change.data["reason"] not in valid_reasons:
            return ["Not a valid optout reason"]
        else:
            return []

    def check_momconnect_loss_optout_reason(self, data_fields, change):
        loss_reasons = ["miscarriage", "stillbirth", "babyloss"]
        if "reason" not in data_fields:
            return ["Optout reason is missing"]
        elif change.data["reason"] not in loss_reasons:
            return ["Not a valid loss reason"]
        else:
            return []

    def check_momconnect_nonloss_optout_reason(self, data_fields, change):
        nonloss_reasons = ["not_useful", "other", "unknown", "sms_failure"]
        if "reason" not in data_fields:
            return ["Optout reason is missing"]
        elif change.data["reason"] not in nonloss_reasons:
            return ["Not a valid nonloss reason"]
        else:
            return []

    def check_momconnect_change_language(self, data_fields, change):
        """
        Ensures that the provided language is a valid language choice.
        """
        if "language" not in data_fields:
            return ["language field is missing"]
        elif not utils.is_valid_lang(change.data['language']):
            return ["Not a valid language choice"]
        else:
            return []

    def check_momconnect_change_msisdn(self, data_fields, change):
        """
        Ensures that a new msisdn is provided, and that it is a valid msisdn.
        """
        if "msisdn" not in data_fields:
            return ["msisdn field is missing"]
        elif not utils.is_valid_msisdn(change.data['msisdn']):
            return ["Not a valid MSISDN"]
        else:
            return []

    def check_momconnect_change_identification(self, data_fields, change):
        if "id_type" not in data_fields:
            return ["ID type missing"]

        if change.data["id_type"] == "sa_id":
            if "sa_id_no" not in data_fields:
                return ["SA ID number missing"]
            elif not utils.is_valid_sa_id_no(change.data["sa_id_no"]):
                return ["SA ID number invalid"]
            else:
                return []
        elif change.data["id_type"] == "passport":
            errors = []

            if "passport_no" not in data_fields:
                errors += ["Passport number missing"]
            elif not utils.is_valid_passport_no(change.data["passport_no"]):
                errors += ["Passport number invalid"]

            if "passport_origin" not in data_fields:
                errors += ["Passport origin missing"]
            elif not utils.is_valid_passport_origin(
                    change.data["passport_origin"]):
                errors += ["Passport origin invalid"]

            return errors
        else:
            return ["ID type should be 'sa_id' or 'passport'"]

    def check_admin_change_subscription(self, data_fields, change):
        errors = []
        if "messageset" not in data_fields and "language" not in data_fields:
            errors.append(
                "One of these fields must be populated: messageset, language")
        if "subscription" not in data_fields:
            errors.append("Subscription field is missing")
        return errors

    def check_switch_channel(self, data_fields, change):
        """
        Should have the "channel" field, and channel field should be a valid
        channel type
        """
        channel_types = ['whatsapp', 'sms']
        if "channel" not in data_fields:
            return ["'channel' is a required field"]
        elif change.data['channel'] not in channel_types:
            return ["'channel' must be one of {}".format(
                sorted(channel_types))]
        return []

    # Validate
    def validate(self, change):
        """ Validates that all the required info is provided for a
        change.
        """
        self.log.info("Starting change validation")

        validation_errors = []

        # Check if registrant_id is a valid UUID
        if not utils.is_valid_uuid(change.registrant_id):
            validation_errors += ["Invalid UUID registrant_id"]

        # Check that required fields are provided and valid
        data_fields = change.data.keys()

        if 'pmtct_loss' in change.action:
            validation_errors += self.check_pmtct_loss_optout_reason(
                data_fields, change)

        elif change.action == 'pmtct_nonloss_optout':
            validation_errors += self.check_pmtct_nonloss_optout_reason(
                data_fields, change)

        elif change.action == 'nurse_update_detail':
            validation_errors += self.check_nurse_update_detail(
                data_fields, change)

        elif change.action == 'nurse_change_msisdn':
            validation_errors += self.check_nurse_change_msisdn(
                data_fields, change)

        elif change.action == 'nurse_optout':
            validation_errors += self.check_nurse_optout(
                data_fields, change)

        elif 'momconnect_loss' in change.action:
            validation_errors += self.check_momconnect_loss_optout_reason(
                data_fields, change)

        elif change.action == 'momconnect_nonloss_optout':
            validation_errors += self.check_momconnect_nonloss_optout_reason(
                data_fields, change)

        elif change.action == 'momconnect_change_language':
            validation_errors += self.check_momconnect_change_language(
                data_fields, change)

        elif change.action == 'momconnect_change_msisdn':
            validation_errors += self.check_momconnect_change_msisdn(
                data_fields, change)

        elif change.action == 'momconnect_change_identification':
            validation_errors += self.check_momconnect_change_identification(
                data_fields, change)

        elif change.action == 'admin_change_subscription':
            validation_errors += self.check_admin_change_subscription(
                data_fields, change)

        elif change.action == 'switch_channel':
            validation_errors += self.check_switch_channel(
                data_fields, change)

        # Evaluate if there were any problems, save and return
        if len(validation_errors) == 0:
            self.log.info("Change validated successfully - updating change "
                          "object")
            change.validated = True
            change.save()
            return True
        else:
            self.log.info("Change validation failed - updating change object")
            change.data["invalid_fields"] = validation_errors
            change.save()
            return False

    # Run
    def run(self, change_id, **kwargs):
        """ Implements the appropriate action
        """
        self.log = self.get_logger(**kwargs)
        self.log.info("Looking up the change")
        change = Change.objects.get(id=change_id)
        change_validates = self.validate(change)

        if change_validates:
            submit_task = {
                'baby_switch': self.baby_switch,
                'pmtct_loss_switch': self.pmtct_loss_switch,
                'pmtct_loss_optout': self.pmtct_loss_optout,
                'pmtct_nonloss_optout': self.pmtct_nonloss_optout,
                'nurse_update_detail': self.nurse_update_detail,
                'nurse_change_msisdn': self.nurse_change_msisdn,
                'nurse_optout': self.nurse_optout,
                'momconnect_loss_switch': self.momconnect_loss_switch,
                'momconnect_loss_optout': self.momconnect_loss_optout,
                'momconnect_nonloss_optout': self.momconnect_nonloss_optout,
                'momconnect_change_language': self.momconnect_change_language,
                'momconnect_change_msisdn': self.momconnect_change_msisdn,
                'momconnect_change_identification': (
                    self.momconnect_change_identification),
                'admin_change_subscription': self.admin_change_subscription,
                'switch_channel': self.switch_channel,
            }.get(change.action, None)(change)

            if submit_task is not None:
                task = chain(
                    submit_task,
                    remove_personally_identifiable_fields.si(str(change.pk)))
                task.delay()
            self.log.info("Task executed successfully")
            return True
        else:
            self.log.info("Task terminated due to validation issues")
            return False


validate_implement = ValidateImplement()


@app.task()
def remove_personally_identifiable_fields(change_id):
    """
    Saves the personally identifiable fields to the identity, and then
    removes them from the change object.
    """
    change = Change.objects.get(id=change_id)

    fields = set((
        'id_type', 'dob', 'passport_no', 'passport_origin', 'sa_id_no',
        'persal_no', 'sanc_no')).intersection(change.data.keys())
    if fields:
        identity = is_client.get_identity(change.registrant_id)

        for field in fields:
            identity['details'][field] = change.data.pop(field)

        is_client.update_identity(
            identity['id'], {'details': identity['details']})

    msisdn_fields = set((
        'msisdn_device', 'msisdn_new', 'msisdn_old')
        ).intersection(change.data.keys())
    for field in msisdn_fields:
        msisdn = change.data.pop(field)
        identities = is_client.get_identity_by_address('msisdn', msisdn)
        try:
            field_identity = next(identities['results'])
        except StopIteration:
            field_identity = is_client.create_identity({
                'details': {
                    'addresses': {
                        'msisdn': {msisdn: {}},
                    },
                },
            })
        field = field.replace('msisdn', 'uuid')
        change.data[field] = field_identity['id']

    change.save()


def restore_personally_identifiable_fields(change):
    """
    Looks up the required information from the identity store, and places it
    back on the change object.

    This function doesn't save the changes to the database, but instead
    returns that Change object with the required information on it.
    """
    identity = is_client.get_identity(change.registrant_id)
    if not identity:
        return change

    fields = set((
        'id_type', 'dob', 'passport_no', 'passport_origin', 'sa_id_no',
        'persal_no', 'sanc_no'))\
        .intersection(identity['details'].keys())\
        .difference(change.data.keys())
    for field in fields:
        change.data[field] = identity['details'][field]

    uuid_fields = set((
        'uuid_device', 'uuid_new', 'uuid_old')
        ).intersection(change.data.keys())
    for field in uuid_fields:
        msisdn = utils.get_identity_msisdn(change.data[field])
        if msisdn:
            field = field.replace('uuid', 'msisdn')
            change.data[field] = msisdn

    return change


class BasePushOptoutToJembi(object):
    def get_today(self):
        return datetime.datetime.today()

    def get_timestamp(self, change):
        return change.created_at.strftime("%Y%m%d%H%M%S")

    def get_optout_reason(self, reason):
        """
        Returns the numerical Jembi optout reason for the given optout reason.
        """
        return {
            'miscarriage': 1,
            'stillbirth': 2,
            'babyloss': 3,
            'not_useful': 4,
            'other': 5,
            'unknown': 6,
            'job_change': 7,
            'number_owner_change': 8,
            'not_hiv_pos': 9,
            'sms_failure': 10,
        }.get(reason)

    def get_identity_address(self, identity, address_type='msisdn'):
        """
        Returns a single address for the identity for the given address
        type.
        """
        addresses = identity.get(
            'details', {}).get('addresses', {}).get(address_type, {})
        address = None
        for address, details in iteritems(addresses):
            if details.get('default'):
                return address
        return address

    def run(self, change_id, **kwargs):
        from .models import Change
        change = Change.objects.get(pk=change_id)
        json_doc = self.build_jembi_json(change)
        try:
            result = requests.post(
                self.URL,
                headers={'Content-Type': 'application/json'},
                data=json.dumps(json_doc),
                auth=(settings.JEMBI_USERNAME, settings.JEMBI_PASSWORD),
                verify=False
            )
            result.raise_for_status()
            return result.text
        except (HTTPError,) as e:
            # retry message sending if in 500 range (3 default retries)
            if 500 < e.response.status_code < 599:
                raise self.retry(exc=e)
            else:
                self.log.error('Error when posting to Jembi. Payload: %r' % (
                    json_doc))
                raise e
        except (Exception,) as e:
            self.log.error(
                'Problem posting Optout %s JSON to Jembi' % (
                    change_id), exc_info=True)


class PushMomconnectOptoutToJembi(BasePushOptoutToJembi, Task):
    """
    Sends a momconnect optout change to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_momconnect_optout_to_jembi"
    log = get_task_logger(__name__)
    URL = urljoin(settings.JEMBI_BASE_URL, 'optout')

    def build_jembi_json(self, change):
        identity = is_client.get_identity(change.registrant_id) or {}
        address = self.get_identity_address(identity)
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'cmsisdn': address,
            'dmsisdn': address,
            'type': 4,
            'optoutreason': self.get_optout_reason(change.data['reason']),
        }


push_momconnect_optout_to_jembi = PushMomconnectOptoutToJembi()


class PushPMTCTOptoutToJembi(PushMomconnectOptoutToJembi, Task):
    """
    Sends a PMTCT optout change to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_pmtct_optout_to_jembi"
    URL = urljoin(settings.JEMBI_BASE_URL, 'pmtctOptout')

    def build_jembi_json(self, change):
        identity = is_client.get_identity(change.registrant_id) or {}
        address = self.get_identity_address(identity)
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'cmsisdn': address,
            'dmsisdn': address,
            'type': 10,
            'optoutreason': self.get_optout_reason(change.data['reason']),
        }


push_pmtct_optout_to_jembi = PushPMTCTOptoutToJembi()


class PushMomconnectBabyLossToJembi(BasePushOptoutToJembi, Task):
    """
    Sends a momconnect baby loss change to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_momconnect_babyloss_to_jembi"
    log = get_task_logger(__name__)
    URL = urljoin(settings.JEMBI_BASE_URL, 'subscription')

    def build_jembi_json(self, change):
        identity = is_client.get_identity(change.registrant_id) or {}
        address = self.get_identity_address(identity)
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'cmsisdn': address,
            'dmsisdn': address,
            'type': 5,
        }


push_momconnect_babyloss_to_jembi = PushMomconnectBabyLossToJembi()


class PushMomconnectBabySwitchToJembi(BasePushOptoutToJembi, Task):
    """
    Sends a momconnect baby switch change to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_momconnect_babyswitch_to_jembi"
    log = get_task_logger(__name__)
    URL = urljoin(settings.JEMBI_BASE_URL, 'subscription')

    def build_jembi_json(self, change):
        identity = is_client.get_identity(change.registrant_id) or {}
        address = self.get_identity_address(identity)
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'cmsisdn': address,
            'dmsisdn': address,
            'type': 11,
        }


push_momconnect_babyswitch_to_jembi = PushMomconnectBabySwitchToJembi()


class PushNurseconnectOptoutToJembi(BasePushOptoutToJembi, Task):
    """
    Sends a nurseconnect optout change to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_nurseconnect_optout_to_jembi"
    log = get_task_logger(__name__)
    URL = urljoin(settings.JEMBI_BASE_URL, 'nc/optout')

    def get_nurse_id(
            self, id_type, id_no=None, passport_origin=None, mom_msisdn=None):
        if id_type == 'sa_id':
            return id_no + "^^^ZAF^NI"
        elif id_type == 'passport':
            return id_no + '^^^' + passport_origin.upper() + '^PPN'
        else:
            return mom_msisdn.replace('+', '') + '^^^ZAF^TEL'

    def get_dob(self, nurse_dob):
        return nurse_dob.strftime("%Y%m%d")

    def get_nurse_registration(self, change):
        """
        A lot of the data that we need to send for the optout is contained
        in the registration, so we need to get the latest registration.
        """
        reg = Registration.objects\
            .filter(registrant_id=change.registrant_id)\
            .order_by('-created_at')\
            .first()
        if reg.data.get("msisdn_registrant", None) is None:
            reg = add_personally_identifiable_fields(reg)
        return reg

    def build_jembi_json(self, change):
        registration = self.get_nurse_registration(change)
        if registration is None:
            self.log.error(
                'Cannot find registration for change {}'.format(change.pk))
            return
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'type': 8,
            "cmsisdn": registration.data['msisdn_registrant'],
            "dmsisdn": registration.data['msisdn_device'],
            'rmsisdn': None,
            "faccode": registration.data['faccode'],
            "id": self.get_nurse_id(
                registration.data.get('id_type'),
                (registration.data.get('sa_id_no')
                 if registration.data.get('id_type') == 'sa_id'
                 else registration.data.get('passport_no')),
                # passport_origin may be None if sa_id is used
                registration.data.get('passport_origin'),
                registration.data['msisdn_registrant']),
            "dob": (self.get_dob(
                datetime.strptime(registration.data['mom_dob'], '%Y-%m-%d'))
                    if registration.data.get('mom_db')
                    else None),
            'optoutreason': self.get_optout_reason(change.data['reason']),
        }


push_nurseconnect_optout_to_jembi = PushNurseconnectOptoutToJembi()


class PushChannelSwitchToJembi(BasePushOptoutToJembi, Task):
    """
    Sends a channel switch to Jembi.
    """
    name = "ndoh_hub.changes.tasks.push_channel_switch_to_jembi"
    log = get_task_logger(__name__)
    URL = urljoin(settings.JEMBI_BASE_URL, 'messageChange')

    def build_jembi_json(self, change):
        identity = is_client.get_identity(change.registrant_id) or {}
        address = self.get_identity_address(identity)
        return {
            'encdate': self.get_timestamp(change),
            'mha': 1,
            'swt': 1,
            'cmsisdn': address,
            'dmsisdn': address,
            'type': 12,
            'channel_current': change.data['old_channel'],
            'channel_new': change.data['channel'],
        }


push_channel_switch_to_jembi = PushChannelSwitchToJembi()


class ProcessWhatsAppUnsentEvent(Task):
    """
    Switches a user to SMS messaging if we couldn't successfully send them a
    message on WhatsApp
    """
    name = "ndoh_hub.changes.tasks.process_whatsapp_unsent_event"

    def run(self, vumi_message_id: str, user_id: str, **kwargs) -> None:
        source_id: int = Source.objects.values('pk').get(user=user_id)['pk']
        try:
            identity_uuid: str = next(utils.ms_client.get_outbounds({
                'vumi_message_id': vumi_message_id,
            })['results'])['to_identity']
        except StopIteration:
            """
            Outbound with message id doesn't exist, so don't create a change
            """
            return

        Change.objects.create(
            registrant_id=identity_uuid,
            action='switch_channel',
            data={
                'channel': "sms",
                'reason': "whatsapp_unsent_event",
            },
            source_id=source_id,
            created_by_id=user_id,
        )


process_whatsapp_unsent_event = ProcessWhatsAppUnsentEvent()
