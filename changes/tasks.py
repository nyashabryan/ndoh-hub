from celery.task import Task
from django.conf import settings
from seed_services_client.stage_based_messaging import StageBasedMessagingApiClient  # noqa

from ndoh_hub import utils
from .models import Change
from registrations.models import SubscriptionRequest


sbm_client = StageBasedMessagingApiClient(
    api_url=settings.STAGE_BASED_MESSAGING_URL,
    auth_token=settings.STAGE_BASED_MESSAGING_TOKEN
)


class ValidateImplement(Task):
    """ Task to apply a Change action.
    """
    name = "ndoh_hub.changes.tasks.validate_implement"

    # Action implementation
    def baby_switch(self, change):
        """ This should be applied when a mother has her baby. Currently it
        only changes the pmtct subscription, but in the future it will also
        change her momconnect subscription.
        """
        # Get current subscriptions
        active_subs = sbm_client.get_subscriptions(
            {'id': change.registrant_id, 'active': True}
        )["results"]
        # Determine if the mother has an active pmtct subscription and
        # deactivate active subscriptions
        has_active_pmtct_sub = False

        for active_sub in active_subs:
            # get the messageset and check if it is pmtct
            messageset = sbm_client.get_messageset(active_sub["messageset"])
            if "pmtct" in messageset["short_name"]:
                has_active_pmtct_sub = True
                lang = active_sub["lang"]

            sbm_client.update_subscription(active_sub["id"], {"active": False})

        if has_active_pmtct_sub:
            # create a postbirth pmtct subscriptionrequest

            # . determine messageset shortname
            short_name = utils.get_messageset_short_name(
                "pmtct_postbirth", "patient", 0)

            # . determine sbm details
            msgset_id, msgset_schedule, next_sequence_number =\
                utils.get_messageset_schedule_sequence(
                    short_name, 0)

            subscription = {
                "identity": change.registrant_id,
                "messageset": msgset_id,
                "next_sequence_number": next_sequence_number,
                "lang": lang,
                "schedule": msgset_schedule
            }
            SubscriptionRequest.objects.create(**subscription)

        # Future: create a postbirth momconnect subscriptionrequest

        return "Switch to baby completed"

    def pmtct_loss_switch(self, change):
        """ The rest of the action required (deactivating momconnect
        subscription, subscribing to loss messages) is currently done on the
        old system via the ndoh-jsbox ussd_pmtct app, we're only deactivating
        the subscriptions here.
        """
        # Get current subscriptions
        active_subs = sbm_client.get_subscriptions(
            {'id': change.registrant_id, 'active': True}
        )["results"]
        # Deactivate subscriptions
        for active_sub in active_subs:
            sbm_client.update_subscription(active_sub["id"], {"active": False})

        return "PMTCT switch to loss completed"

    def pmtct_loss_optout(self, change):
        """ The rest of the action required (opting out the identity on the
        identity store, opting out the vumi contact, deactivating the old
        system subscriptions) is currently done via the ndoh-jsbox ussd_pmtct
        app, we're only deactivating the subscriptions here.
        """
        # Get current subscriptions
        active_subs = sbm_client.get_subscriptions(
            {'id': change.registrant_id, 'active': True}
        )["results"]
        # Deactivate subscriptions
        for active_sub in active_subs:
            sbm_client.update_subscription(active_sub["id"], {"active": False})

        return "PMTCT optout due to loss completed"

    def pmtct_nonloss_optout(self, change):
        """ The rest of the action required (opting out the identity on the
        identity store, opting out the vumi contact, deactivating the old
        system subscriptions) is currently done via the ndoh-jsbox ussd_pmtct
        app, we're only deactivating the subscriptions here.
        """
        # Get current subscriptions
        active_subs = sbm_client.get_subscriptions(
            {'id': change.registrant_id, 'active': True}
        )["results"]
        # Deactivate subscriptions
        for active_sub in active_subs:
            sbm_client.update_subscription(active_sub["id"], {"active": False})

        return "PMTCT optout not due to loss completed"

    def nurse_update_detail(self, change):
        """ This currently does nothing, but in a seperate issue this will
        handle sending the information update to Jembi
        """
        return "NurseConnect detail updated"

    def nurse_change_msisdn(self, change):
        """ This currently does nothing, but in a seperate issue this will
        handle sending the information update to Jembi
        """
        return "NurseConnect msisdn changed"

    def nurse_optout(self, change):
        """ The rest of the action required (opting out the identity on the
        identity store) is currently done via the ndoh-jsbox ussd_nurse
        app, we're only deactivating the subscriptions here. Note this only
        deactivates the NurseConnect subscription.
        """
        # Get nurseconnect messageset
        messageset = sbm_client.get_messagesets(
            {"short_name": "nurseconnect.hw_full.1"})["results"][0]
        # Get current subscriptions
        active_subs = sbm_client.get_subscriptions({
            'id': change.registrant_id, 'active': True,
            'messageset': messageset["id"]}
        )["results"]
        # Deactivate subscriptions
        for active_sub in active_subs:
            sbm_client.update_subscription(active_sub["id"], {"active": False})

        return "Nurse optout completed"

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

    # Validate
    def validate(self, change):
        """ Validates that all the required info is provided for a
        change.
        """
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

        # Evaluate if there were any problems, save and return
        if len(validation_errors) == 0:
            change.validated = True
            change.save()
            return True
        else:
            change.data["invalid_fields"] = validation_errors
            change.save()
            return False

    # Run
    def run(self, change_id, **kwargs):
        """ Implements the appropriate action
        """
        change = Change.objects.get(id=change_id)
        change_validates = self.validate(change)

        if change_validates:
            {
                'baby_switch': self.baby_switch,
                'pmtct_loss_switch': self.pmtct_loss_switch,
                'pmtct_loss_optout': self.pmtct_loss_optout,
                'pmtct_nonloss_optout': self.pmtct_nonloss_optout,
                'nurse_update_detail': self.nurse_update_detail,
                'nurse_change_msisdn': self.nurse_change_msisdn,
                'nurse_optout': self.nurse_optout,
            }.get(change.action, None)(change)
            return True
        else:
            return False

validate_implement = ValidateImplement()
