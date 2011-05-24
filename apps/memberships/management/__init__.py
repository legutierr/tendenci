from django.db.models.signals import post_syncdb
from django.utils.translation import ugettext_noop as _

from notification import models as notification

def create_notice_types(app, created_models, verbosity, **kwargs):

    notification.create_notice_type(
        'membership_approved_to_admin',
        _('Membership Application Approved'),
        _('Membership Application Approved'))

    notification.create_notice_type(
        'membership_disapproved_to_admin',
        _('Membership Application Disapproved'),
        _('Membership Application Disapproved'))

    notification.create_notice_type(
        'membership_approved_to_member',
        _('Membership Application Approved'),
        _('Membership Application Approved'))

    notification.create_notice_type(
        'membership_disapproved_to_member',
        _('Membership Application Disapproved'),
        _('Membership Application Disapproved'))

post_syncdb.connect(create_notice_types, sender=notification)