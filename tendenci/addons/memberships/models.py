import re
import hashlib
import uuid
import time
from functools import partial
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from django.db import models
from django.db.models.query_utils import Q
from django.template import Context, Template
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.contrib.contenttypes import generic

from tendenci.core.base.utils import day_validate
from tendenci.core.site_settings.utils import get_setting
from tendenci.core.perms.models import TendenciBaseModel
from tendenci.core.perms.utils import get_notice_recipients, has_perm
from tendenci.core.perms.object_perms import ObjectPermission
from tendenci.apps.invoices.models import Invoice
from tendenci.apps.user_groups.models import Group
from tendenci.addons.memberships.managers import MembershipManager, \
    MemberAppManager, MemberAppEntryManager
from tendenci.core.base.utils import fieldify
from tinymce import models as tinymce_models
from tendenci.core.payments.models import PaymentMethod
from tendenci.apps.user_groups.models import GroupMembership
from tendenci.core.event_logs.models import EventLog
from tendenci.apps.profiles.models import Profile
from tendenci.core.files.models import File

from south.modelsinspector import add_introspection_rules
add_introspection_rules([], ["^tinymce.models.HTMLField"])
add_introspection_rules([], ["^tendenci.core.base.fields.SlugField"])

FIELD_CHOICES = (
    ("text", _("Text")),
    ("paragraph-text", _("Paragraph Text")),
    ("check-box", _("Checkbox")),
    ("choose-from-list", _("Choose from a list")),
    ("multi-select", _("Multi select")),
    ("file-uploader", _("File upload")),
    ("date", _("Date")),
    ("date-time", _("Date/time")),
    ("first-name", _("First Name")),
    ("last-name", _("Last Name")),
    ("email", _("Email")),
    ("header", _("Section Header")),
    ("description", _(" Description")),
    ("horizontal-rule", _("Horizontal Rule")),
    ("membership-type", _("Membership Type")),
    ("payment-method", _("Payment Method")),
    ("corporate_membership_id", _("Corporate Membership ID")),
)

OBJECT_TYPE_CHOICES = (
    ("user", _("User")),
    ("membership", _("Membership")),
    ("directory", _("Directory")),
    ("donation", _("Donation")),
    ("custom", _("Custom")),
)
PERIOD_CHOICES = (
    ("fixed", _("Fixed")),
    ("rolling", _("Rolling")),
)
PERIOD_UNIT_CHOICES = (
    ("days", _("Days")),
    ("months", _("Months")),
    ("years", _("Years")),
)
FIELD_FUNCTIONS = (
    ("Group", _("Subscribe to Group")),
)


class MembershipType(TendenciBaseModel):
    guid = models.CharField(max_length=50)
    name = models.CharField(_('Name'), max_length=255, unique=True)
    description = models.CharField(_('Description'), max_length=500)
    price = models.DecimalField(_('Price'), max_digits=15, decimal_places=2, blank=True, default=0,
        help_text="Set 0 for free membership.")
    renewal_price = models.DecimalField(_('Renewal Price'), max_digits=15, decimal_places=2,
        blank=True, default=0, null=True, help_text="Set 0 for free membership.")
    # for first time processing
    admin_fee = models.DecimalField(_('Admin Fee'),
        max_digits=15, decimal_places=2, blank=True, default=0, null=True,
        help_text="Admin fee for the first time processing")

    group = models.ForeignKey(Group, related_name="membership_types",
        help_text="Members joined will be added to this group")

    require_approval = models.BooleanField(_('Require Approval'), default=1)
    allow_renewal = models.BooleanField(_('Allow Renewal'), default=1)
    renewal = models.BooleanField(_('Renewal Only'), default=0)
    renewal_require_approval = models.BooleanField(_('Renewal Requires Approval'), default=1)

    order = models.IntegerField(_('Order'), default=0,
        help_text='Types will be displayed in ascending order based on this field')
    admin_only = models.BooleanField(_('Admin Only'), default=0)  # from allowuseroption

    never_expires = models.BooleanField(_("Never Expires"), default=0,
                                        help_text='If selected, skip the Renewal Options.')
    period = models.IntegerField(_('Period'), default=0)
    period_unit = models.CharField(choices=PERIOD_UNIT_CHOICES, max_length=10)
    period_type = models.CharField(_("Period Type"), default='rolling', choices=PERIOD_CHOICES, max_length=10)

    rolling_option = models.CharField(_('Expires On'), max_length=50)
    rolling_option1_day = models.IntegerField(_('Expiration Day'), default=0)
    rolling_renew_option = models.CharField(_('Renewal Expires On'), max_length=50)
    rolling_renew_option1_day = models.IntegerField(default=0)
    rolling_renew_option2_day = models.IntegerField(default=0)

    fixed_option = models.CharField(_('Expires On'), max_length=50)
    fixed_option1_day = models.IntegerField(default=0)
    fixed_option1_month = models.IntegerField(default=0)
    fixed_option1_year = models.IntegerField(default=0)
    fixed_option2_day = models.IntegerField(default=0)
    fixed_option2_month = models.IntegerField(default=0)

    fixed_option2_can_rollover = models.BooleanField(_("Allow Rollover"), default=0)
    fixed_option2_rollover_days = models.IntegerField(default=0,
            help_text=_("Membership signups after this date covers the following calendar year as well."))

    renewal_period_start = models.IntegerField(_('Renewal Period Start'), default=30,
            help_text="How long (in days) before the memberships expires can the member renew their membership.")
    renewal_period_end = models.IntegerField(_('Renewal Period End'), default=30,
            help_text="How long (in days) after the memberships expires can the member renew their membership.")
    expiration_grace_period = models.IntegerField(_('Expiration Grace Period'), default=0,
            help_text="The number of days after the membership expires their membership is still active.")

    class Meta:
        verbose_name = "Membership Type"
        permissions = (("view_membershiptype", "Can view membership type"),)

    def __unicode__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.id:
            self.guid = str(uuid.uuid1())
        super(MembershipType, self).save(*args, **kwargs)

    def get_expiration_dt(self, renewal=False, join_dt=None, renew_dt=None):
        """
        Calculate the expiration date - for join or renew (renewal=True)

        Examples:

            For join:
            expiration_dt = membership_type.get_expiration_dt(join_dt=membership.join_dt)

            For renew:
            expiration_dt = membership_type.get_expiration_dt(renewal=True,
                                                              join_dt=membership.join_dt,
                                                              renew_dt=membership.renew_dt)
        """
        now = datetime.now()

        if not join_dt or not isinstance(join_dt, datetime):
            join_dt = now
        if renewal and (not renew_dt or not isinstance(renew_dt, datetime)):
            renew_dt = now

        if self.never_expires:
            return None

        if self.period_type == 'rolling':
            if self.period_unit == 'days':
                return now + timedelta(days=self.period)

            elif self.period_unit == 'months':
                return now + relativedelta(months=self.period)

            else:  # if self.period_unit == 'years':
                if not renewal:
                    if self.rolling_option == '0':
                        # expires on end of full period
                        return join_dt + relativedelta(years=self.period)
                    else:  # self.expiration_method == '1':
                        # expires on ? days at signup (join) month
                        if not self.rolling_option1_day:
                            self.rolling_option1_day = 1
                        expiration_dt = join_dt + relativedelta(years=self.period)
                        self.rolling_option1_day = day_validate(datetime(expiration_dt.year, join_dt.month, 1),
                                                                    self.rolling_option1_day)

                        return datetime(expiration_dt.year, join_dt.month,
                            self.rolling_option1_day, expiration_dt.hour, expiration_dt.minute, expiration_dt.second)

                else:  # renewal = True
                    if self.rolling_renew_option == '0':
                        # expires on the end of full period
                        return renew_dt + relativedelta(years=self.period)
                    elif self.rolling_renew_option == '1':
                        # expires on the ? days at signup (join) month
                        if not self.rolling_renew_option1_day:
                            self.rolling_renew_option1_day = 1
                        expiration_dt = renew_dt + relativedelta(years=self.period)
                        self.rolling_renew_option1_day = day_validate(datetime(expiration_dt.year, join_dt.month, 1),
                            self.rolling_renew_option1_day)

                        return datetime(expiration_dt.year, join_dt.month,
                                                 self.rolling_renew_option1_day, expiration_dt.hour,
                                                 expiration_dt.minute, expiration_dt.second)
                    else:
                        # expires on the ? days at renewal month
                        if not self.rolling_renew_option2_day:
                            self.rolling_renew_option2_day = 1
                        expiration_dt = renew_dt + relativedelta(years=self.period)
                        self.rolling_renew_option2_day = day_validate(datetime(expiration_dt.year, renew_dt.month, 1),
                            self.rolling_renew_option2_day)

                        return datetime(expiration_dt.year, renew_dt.month, self.rolling_renew_option2_day, expiration_dt.hour,
                            expiration_dt.minute, expiration_dt.second)

        else:  # self.period_type == 'fixed':
            if self.fixed_option == '0':
                # expired on the fixed day, fixed month, fixed year
                if not self.fixed_option1_day:
                    self.fixed_option1_day = 1
                if not self.fixed_option1_month:
                    self.fixed_option1_month = 1
                if self.fixed_option1_month > 12:
                    self.fixed_option1_month = 12
                if not self.fixed_option1_year:
                    self.fixed_option1_year = now.year

                self.fixed_option1_day = day_validate(datetime(self.fixed_option1_year,
                    self.fixed_option1_month, 1), self.fixed_option1_day)

                return datetime(self.fixed_option1_year, self.fixed_option1_month,
                    self.fixed_option1_day)

            else:  # self.fixed_option == '1'
                # expired on the fixed day, fixed month of current year
                if not self.fixed_option2_day:
                    self.fixed_option2_day = 1
                if not self.fixed_option2_month:
                    self.fixed_option2_month = 1
                if self.fixed_option2_month > 12:
                    self.fixed_option2_month = 12

                self.fixed_option2_day = day_validate(datetime(now.year,
                    self.fixed_option2_month, 1), self.fixed_option2_day)

                expiration_dt = datetime(now.year, self.fixed_option2_month,
                                        self.fixed_option2_day)
                if self.fixed_option2_can_rollover:
                    if not self.fixed_option2_rollover_days:
                        self.fixed_option2_rollover_days = 0
                    if (now - expiration_dt).days <= self.fixed_option2_rollover_days:
                        expiration_dt = expiration_dt + relativedelta(years=1)

                return expiration_dt


class Membership(TendenciBaseModel):
    """
    Holds all membership records.
    Memberships that are approved, denied, and pending.
    """
    guid = models.CharField(max_length=50)
    member_number = models.CharField(_("Member Number"), max_length=50)
    membership_type = models.ForeignKey("MembershipType", verbose_name=_("Membership Type"))
    user = models.ForeignKey(User, related_name="memberships")
    renewal = models.BooleanField(default=False)
    invoice = models.ForeignKey(Invoice, blank=True, null=True)
    subscribe_dt = models.DateTimeField(_("Subscribe Date"))
    expire_dt = models.DateTimeField(_("Expiration Date Time"), null=True)
    corporate_membership_id = models.IntegerField(_('Corporate Membership Id'), default=0)
    payment_method = models.ForeignKey(PaymentMethod, blank=True, null=True)
    ma = models.ForeignKey("App", null=True)
    send_notice = models.BooleanField(default=True)

    perms = generic.GenericRelation(ObjectPermission,
        object_id_field="object_id", content_type_field="content_type")

    objects = MembershipManager()

    class Meta:
        verbose_name = _("Member")
        verbose_name_plural = _("Members")
        permissions = (("view_membership", "Can view membership"),)

    def __unicode__(self):
        label = u''
        if hasattr(self, 'user'):
            label = self.user.get_full_name() or self.user.username
        label = "#%s %s" % (self.member_number, label)
        return label.strip()

    @models.permalink
    def get_absolute_url(self):
        return ('membership.details', [self.pk])

    def save(self, *args, **kwargs):
        self.guid = self.guid or unicode(uuid.uuid1())
        super(Membership, self).save(*args, **kwargs)

    def is_active(self):
        """
        status = True, status_detail = 'active', and has not expired
        considers grace period when evaluating expiration date-time
        """
        from dateutil.relativedelta import relativedelta
        grace_period = self.membership_type.expiration_grace_period
        graceful_now = datetime.now() - relativedelta(days=grace_period)

        if self.status and self.status_detail.lower() == 'active':

            # membership does not expire
            if not self.expire_dt:
                return True

            # membership has not expired
            if self.expire_dt > graceful_now:
                return True

        return False

    def get_expire_dt(self):
        from dateutil.relativedelta import relativedelta
        grace_period = self.membership_type.expiration_grace_period
        return self.expire_dt + relativedelta(days=grace_period)

    def get_name(self):

        user = self.user
        profile = user.get_profile()

        name = "%s %s" % (user.first_name, user.last_name)
        name = name.strip()

        return profile.display_name or name or user.email or user.username

    def get_entry(self):
        try:
            entry = self.entries.filter(is_approved=True).order_by('decision_dt')[0]
        except (ObjectDoesNotExist, MultipleObjectsReturned, IndexError):
            entry = None
        return entry

    @property
    def entry_items(self):
        """
        Returns a dictionary of entry items.
        The approved entry that is associated with this membership.
        """
        return self.get_entry_items()

    def get_entry_items(self, slugify_label=True):
        items = {}
        entry = self.get_entry()

        if entry:
            for field in entry.fields.all():
                label = field.field.label
                if slugify_label:
                    label = slugify(label).replace('-', '_')
                items[label] = field.value

        return items

    def get_renewal_period_dt(self):
        """
        calculate and return a tuple of renewal period dt (the renewal window):
         (renewal_period_start_dt, renewal_period_end_dt)
        """
        if not self.expire_dt or not isinstance(self.expire_dt, datetime):
            return (None, None)

        start_dt = self.expire_dt - timedelta(days=self.membership_type.renewal_period_start)
        end_dt = self.expire_dt + timedelta(days=self.membership_type.renewal_period_end)

        return (start_dt, end_dt)

    def can_renew(self):
        """
        Checks memberships that are never ending. No expire dt.
        Checks if membership is within renewal period.
        Returns boolean.
        """

        if self.expire_dt is None:  # neverending expirations
            return False

        start_dt, end_dt = self.get_renewal_period_dt()

        # assert that we're within the renewal period
        return (datetime.now() >= start_dt and datetime.now() <= end_dt)

    @classmethod
    def types_in_contract(cls, user):
        """
        Return a list of membership types that this
        user is still in contract with.

        This means that their a member and they are
        not within their renewal period.
        """

        in_contract = []

        if user.is_anonymous():
            return in_contract

        memberships = cls.objects.filter(user=user)
        for membership in memberships:
            if not membership.can_renew() and membership.status_detail == 'active':
                in_contract.append(membership.membership_type)

        return in_contract

    def allow_view_by(self, this_user):
        if this_user.profile.is_superuser:
            return True

        if this_user.is_anonymous():
            if self.allow_anonymous_view:
                return self.status and self.status_detail == 'active'
        else:
            if this_user in (self.creator, self.owner, self.user):
                return self.status and self.status_detail == 'active'
            elif self.allow_user_view:
                return self.status and self.status_detail == 'active'
            elif has_perm(this_user, 'memberships.view_app', self):
                return True

        return False

    def populate_user_member_id(self, verbosity=1):
        """
        Populate the member ID (or member number) to user profile.
        """
        if self.is_active():
            if self.member_number:
                [profile] = Profile.objects.filter(user=self.user)[:1] or [None]
                if not profile:
                    profile = Profile.objects.create_profile(self.user)

                if any([not profile.member_number,
                       profile.member_number != self.member_number]):
                    profile.member_number = self.member_number
                    profile.save()

                    # set the is_member attr to True for this user
                    setattr(self.user, 'is_member', True)

                    if verbosity > 1:
                        print 'Added member number %s for %s.' % \
                            (self.member_number, self.user.username)
                else:
                    if verbosity > 1:
                        print 'Member number already populated for %s' % self.user.username
            else:
                if verbosity > 1:
                    print '***Membership (ID=%d) does NOT have a member number.' % self.id

    def clear_user_member_id(self):
        """
        Clear the member ID (or member number) in user's profile.
        """
        if not self.is_active():
            [profile] = Profile.objects.filter(user=self.user)[:1] or [None]
            if profile and profile.member_number:
                profile.member_number = u''
                profile.save()

                # set the is_member attr to False for this user
                setattr(self.user, 'is_member', False)

    def populate_or_clear_member_id(self):
        """
        If the membership is active, populate the member ID to profile.
        Otherwise, clear the member ID from profile.
        """
        if self.is_active():
            self.populate_user_member_id()
        else:
            self.clear_user_member_id()
            # set the is_member attr to False for this user
            setattr(self.user, 'is_member', False)



class MembershipImport(models.Model):
    INTERACTIVE_CHOICES = (
        (1, 'Interactive'),
        (0, 'Not Interactive (no login)'),
    )

    OVERRIDE_CHOICES = (
        (0, 'Blank Fields'),
        (1, 'All Fields (override)'),
    )

    KEY_CHOICES = (
        ('email', 'email'),
        ('first_name,last_name,email', 'first_name and last_name and email'),
        ('first_name,last_name,phone', 'first_name and last_name and phone'),
        ('first_name,last_name,company', 'first_name and last_name and company'),
        ('username', 'username'),
    )

    app = models.ForeignKey('App')
    # active users
    interactive = models.IntegerField(choices=INTERACTIVE_CHOICES, default=0)
    # overwrite already existing fields if match
    override = models.IntegerField(choices=OVERRIDE_CHOICES, default=0)
    # uniqueness key
    key = models.CharField(max_length=50, choices=KEY_CHOICES, default="email")

    creator = models.ForeignKey(User)
    create_dt = models.DateTimeField(auto_now_add=True)

    def get_file(self):
        file = File.objects.get_for_model(self)[0]
        return file

    def __unicode__(self):
        return self.get_file().file.name

NOTICE_TYPES = (
    ('join', 'Join Date'),
    ('renewal', 'Renewal Date'),
    ('expiration', 'Expiration Date'),
    ('approve', 'Approval Date'),
    ('disapprove', 'Disapproval Date'),
)


class Notice(models.Model):
    guid = models.CharField(max_length=50, editable=False)
    notice_name = models.CharField(_("Name"), max_length=250)
    num_days = models.IntegerField(default=0)
    notice_time = models.CharField(_("Notice Time"), max_length=20,
                                   choices=(('before', 'Before'),
                                            ('after', 'After'),
                                            ('attimeof', 'At Time Of')))
    notice_type = models.CharField(_("For Notice Type"), max_length=20, choices=NOTICE_TYPES)
    system_generated = models.BooleanField(_("System Generated"), default=0)
    membership_type = models.ForeignKey(
        "MembershipType",
        blank=True,
        null=True,
        help_text=_("Note that if you \
            don't select a membership type, \
            the notice will go out to all members."
        ))

    subject = models.CharField(max_length=255)
    content_type = models.CharField(_("Content Type"),
                                    choices=(('html', 'HTML'),
                                            ('text', 'Plain Text')),
                                    max_length=10)
    sender = models.EmailField(max_length=255, blank=True, null=True)
    sender_display = models.CharField(max_length=255, blank=True, null=True)
    email_content = tinymce_models.HTMLField(_("Email Content"))

    create_dt = models.DateTimeField(auto_now_add=True)
    update_dt = models.DateTimeField(auto_now=True)
    creator = models.ForeignKey(User, related_name="membership_notice_creator",  null=True)
    creator_username = models.CharField(max_length=50, null=True)
    owner = models.ForeignKey(User, related_name="membership_notice_owner", null=True)
    owner_username = models.CharField(max_length=50, null=True)
    status_detail = models.CharField(choices=(('active', 'Active'), ('admin_hold', 'Admin Hold')),
                                     default='active', max_length=50)
    status = models.BooleanField(default=True)

    def __unicode__(self):
        return self.notice_name

    @property
    def footer(self):
        return """
        This e-mail was generated by Tendenci&reg; Software - a
        web based membership management software solution
        www.tendenci.com developed by Schipul - The Web Marketing Company
        """

    def get_entry_items(self, entry, membership):
        items = {}
        if membership:
            items = membership.entry_items
        else:
            if entry:
                for field in entry.fields.all():
                    label = slugify(field.field.label).replace('-', '_')
                    items[label] = field.value
        return items

    def get_subject(self, entry=None, membership=None):
        """
        Return self.subject replace shortcode (context) variables
        The membership object takes priority over entry object
        """
        context = self.get_entry_items(entry, membership)

        return self.build_notice(self.subject, context=context)

    def get_content(self, entry=None, membership=None):
        """
        Return self.email_content with self.footer appended
        and replace shortcode (context) variables
        """

        # defaults
        global_setting = partial(get_setting, 'site', 'global')
        corporate_msg = ''
        expiration_dt = ''

        context = self.get_entry_items(entry, membership)

        if membership:

            if membership.corporate_membership_id:
                corporate_msg = """
                <br /><br />
                <font color="#FF0000">
                Organizational Members, please contact your company Membership coordinator
                to ensure that your membership is being renewed.
                </font>
                """

            if membership.expire_dt:
                expiration_dt = time.strftime(
                    "%d-%b-%y %I:%M %p",
                    membership.expire_dt.timetuple()
                )

            context.update({
                'membernumber': membership.member_number,
                'membershiptype': membership.membership_type.name,
                'membershiplink': '%s%s'.format(global_setting('siteurl'), membership.get_absolute_url()),
                'renewlink': '%s%s'.format(global_setting('siteurl'), membership.get_absolute_url()),
            })

        context.update({
            'expirationdatetime': expiration_dt,
            'sitecontactname': global_setting('sitecontactname'),
            'sitecontactemail': global_setting('sitecontactemail'),
            'sitedisplayname': global_setting('sitedisplayname'),
            'timesubmitted': time.strftime("%d-%b-%y %I:%M %p", datetime.now().timetuple()),
            'corporatemembernotice': corporate_msg,
        })

        content = "%s\n<br /><br />\n%s" % (self.email_content, self.footer)

        return self.build_notice(content, context=context)

    def build_notice(self, content, *args, **kwargs):
        """
        Replace values in a string and return the updated content
        Values are pulled from membership, user, profile, and site_settings
        In the future, maybe we can pull from the membership application entry
        """
        context = kwargs.get('context') or {}  # get context
        content = fieldify(content)

        context = Context(context)
        template = Template(content)

        return template.render(context)

    @classmethod
    def send_notice(cls, **kwargs):
        """
        Send notice to notice_type specified
        within membership_type specified
        to email addresses specified
        Returns boolean.

        Allowed Notice Types: joined, renewed, approved, disapproved
        """
        from tendenci.apps.notifications import models as notification

        notice_type = kwargs.get('notice_type') or 'joined'
        membership_type = kwargs.get('membership_type')
        membership = kwargs.get('membership')
        emails = kwargs.get('emails') or []
        request = kwargs.get('request')
        entry = kwargs.get('entry')

        if isinstance(emails, basestring):
            emails = [emails]  # expecting list of emails

        # allowed notice types
        if notice_type == 'join':
            template_type = 'joined'
        elif notice_type == 'renewal':
            template_type = 'renewed'
        elif notice_type == 'approve':
            template_type = 'approved'
        elif notice_type == 'disapprove':
            template_type = 'disapproved'
        else:
            return False

        # email list required
        if not emails:
            return False

        field_dict = {
            'notice_time': 'attimeof',
            'notice_type': notice_type,
            'status': True,
            'status_detail': 'active',
        }

        # send to applicant
        for notice in Notice.objects.filter(**field_dict):

            notice_requirments = (
                notice.membership_type == membership_type,
                notice.membership_type == None
            )

            if any(notice_requirments):
                notification.send_emails(
                    emails,
                    'membership_%s_to_member' % template_type, {
                    'subject': notice.get_subject(entry=entry, membership=membership),
                    'content': notice.get_content(entry=entry, membership=membership),
                    'membership_total': Membership.objects.active().count(),
                    'reply_to': notice.sender,
                    'sender': notice.sender,
                    'sender_display': notice.sender_display,
                })

        # send email to admins
        membership_recipients = get_notice_recipients('module', 'memberships', 'membershiprecipients')
        admin_recipients = get_notice_recipients('site', 'global', 'allnoticerecipients')
        recipients = list(set(membership_recipients + admin_recipients))

        if recipients and notification:
            notification.send_emails(recipients,
                'membership_%s_to_admin' % template_type, {
                'entry': entry,
                'request': request,
                'membership_total': Membership.objects.active().count(),
            })

        return True

    @models.permalink
    def get_absolute_url(self):
        return ('membership.notice_email_content', [self.id])

    def save(self, *args, **kwargs):
        if not self.id:
            self.guid = str(uuid.uuid1())
        super(Notice, self).save(*args, **kwargs)


class NoticeLog(models.Model):
    guid = models.CharField(max_length=50, editable=False)
    notice = models.ForeignKey(Notice, related_name="logs")
    notice_sent_dt = models.DateTimeField(auto_now_add=True)
    num_sent = models.IntegerField()


class NoticeLogRecord(models.Model):
    guid = models.CharField(max_length=50, editable=False)
    notice_log = models.ForeignKey(NoticeLog, related_name="log_records")
    membership = models.ForeignKey(Membership, related_name="log_records")
    action_taken = models.BooleanField(default=0)
    action_taken_dt = models.DateTimeField(blank=True, null=True)
    create_dt = models.DateTimeField(auto_now_add=True)


class App(TendenciBaseModel):
    guid = models.CharField(max_length=50, editable=False)

    name = models.CharField(_("Name"), max_length=155)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True,
        help_text="Description of this application. Displays at top of application.")
    confirmation_text = tinymce_models.HTMLField()
    notes = tinymce_models.HTMLField(blank=True)
    use_captcha = models.BooleanField(_("Use Captcha"), default=1)
    membership_types = models.ManyToManyField(MembershipType, verbose_name="Membership Types")
    payment_methods = models.ManyToManyField(PaymentMethod, verbose_name="Payment Methods")

    use_for_corp = models.BooleanField(_("Use for Corporate Individuals"), default=0)

    objects = MemberAppManager()

    class Meta:
        verbose_name = "Membership Application"
        permissions = (("view_app", "Can view membership application"),)

    def __unicode__(self):
        return self.name

    @models.permalink
    def get_absolute_url(self):
        return ('membership.application_details', [self.slug])

    def save(self, *args, **kwargs):
        if not self.id:
            self.guid = str(uuid.uuid1())
        super(App, self).save(*args, **kwargs)

    def get_prefill_kwargs(self, membership=None):
        """
        Prefill this application.
        Possible Parameters: user, membership, entry
        """
        entry = membership.ma.entries.order_by('-pk')[0]
        init_kwargs = [(f.field.pk, f.value) for f in entry.fields.all()]

        return dict(init_kwargs)

    def get_initial_info(self, user):
        """
        Get initial information to pre-populate application.
        First look for a previously submitted application.
        Else get initial user information from user/profile and populate.
        Return an initial-dictionary.
        """
        from django.contrib.contenttypes.models import ContentType

        initial = {}
        if user.is_anonymous():
            return initial

        # querying for previously submitted forms
        entries = user.appentry_set.filter(app=self).order_by('-pk')
        if entries:
            initial = dict([(f.field.pk, f.value) for f in entries[0].fields.all()])
            return initial

        # getting fn, ln, em from user/profile record
        user_ct = ContentType.objects.get_for_model(user)
        for field in self.fields.filter(content_type=user_ct):
            if field.field_type == 'first-name':
                initial['field_%s' % field.pk] = user.first_name
            elif field.field_type == 'last-name':
                initial['field_%s' % field.pk] = user.last_name
            elif field.field_type == 'email':
                initial['field_%s' % field.pk] = user.email

        return initial

    def allow_view_by(self, this_user):
        if this_user.profile.is_superuser:
            return True

        if this_user.is_anonymous():
            if self.allow_anonymous_view:
                return self.status and self.status_detail in ['active', 'published']
        else:
            if this_user in (self.creator, self.owner):
                return self.status and self.status_detail in ['active', 'published']
            elif self.allow_user_view:
                return self.status and self.status_detail in ['active', 'published']
            elif has_perm(this_user, 'memberships.view_app', self):
                return True

        return False


class AppFieldManager(models.Manager):
    """
    Only show visible fields when displaying actual form..
    """
    def visible(self):
        return self.filter(visible=True).order_by('position')

    def non_admin_visible(self):
        return self.filter(visible=True, admin_only=False).order_by('position')


class AppField(models.Model):
    app = models.ForeignKey("App", related_name="fields")
    content_type = models.ForeignKey(ContentType,
        related_name="membership_app_field_set", editable=False, null=True)
    attribute_name = models.CharField(_("Attribute Name"), max_length=300)
    label = models.CharField(_("Label"), max_length=2000)

    description = models.TextField(_("Description"), max_length=200, blank=True)

    help_text = models.CharField(_("Help Text"), max_length=200, blank=True)
    default_value = models.CharField(_("Default Value"), max_length=200, blank=True)
    css_class = models.CharField(_("CSS Class"), max_length=200, blank=True)

    field_name = models.CharField(max_length=100, blank=True, default='')
    field_type = models.CharField(_("Type"), choices=FIELD_CHOICES, max_length=100)
    field_function = models.CharField(_("Special Functionality"),
        choices=FIELD_FUNCTIONS, max_length=64, null=True, blank=True)
    function_params = models.CharField(_("Group Name or Names"),
        max_length=100, null=True, blank=True, help_text="Comma separated if more than one")
    vital = models.BooleanField(_("Vital"), default=False, blank=True)
    required = models.BooleanField(_("Required"), default=False, blank=True)
    visible = models.BooleanField(_("Visible"), default=True, blank=True)
    choices = models.CharField(_("Choices"), max_length=1000, blank=True,
        help_text="Comma separated options where applicable")

    unique = models.BooleanField(_("Unique"), default=False, blank=True)
    admin_only = models.BooleanField(_("Admin Only"), default=False)
    position = models.IntegerField(blank=True)
    exportable = models.BooleanField(_("Exportable"), default=True)

    objects = AppFieldManager()

    def save(self, *args, **kwargs):
        if self.position is None:
            # Append
            try:
                last = AppField.objects.order_by('-position')[0]
                self.position = last.position + 1
            except IndexError:
                # First row
                self.position = 0

        return super(AppField, self).save(*args, **kwargs)

    class Meta:
        verbose_name = _("Field")
        verbose_name_plural = _("Fields")
        ordering = ('position',)

    def __unicode__(self):
        return self.label

    def execute_function(self, entry):
        user = entry.user
        if self.field_function == "Group":
            for val in self.function_params.split(','):
                group = Group.objects.get(name=val)
                try:
                    group_membership = GroupMembership.objects.get(group=group, member=user)
                except GroupMembership.DoesNotExist:
                    group_membership = GroupMembership(group=group, member=user)
                    group_membership.creator_id = user.id
                    group_membership.creator_username = user.username
                    group_membership.role = 'subscriber'
                    group_membership.owner_id = user.id
                    group_membership.owner_username = user.username
                    group_membership.save()


class AppEntry(TendenciBaseModel):
    """
    An entry submitted via a membership application.
    """
    app = models.ForeignKey("App", related_name="entries")
    user = models.ForeignKey(User, null=True)
    membership = models.ForeignKey("Membership", related_name="entries", null=True)
    entry_time = models.DateTimeField(_("Date/Time"))
    hash = models.CharField(max_length=40, null=True, default='')
    is_renewal = models.BooleanField()
    is_approved = models.NullBooleanField(_('Approved'), null=True)
    decision_dt = models.DateTimeField(null=True)
    judge = models.ForeignKey(User, null=True, related_name='entries')
    invoice = models.ForeignKey(Invoice, null=True)
    perms = generic.GenericRelation(ObjectPermission,
                                          object_id_field="object_id",
                                          content_type_field="content_type")

    objects = MemberAppEntryManager()

    class Meta:
        verbose_name = _("Application Entry")
        verbose_name_plural = _("Application Entries")
        permissions = (("view_appentry", "Can view membership application entry"),)

    def __unicode__(self):
        return '%s - Submission #%s' % (self.app, self.pk)

    @models.permalink
    def get_absolute_url(self):
        return ('membership.application_entries', [self.pk])

    def allow_view_by(self, this_user):
        if this_user.profile.is_superuser:
            return True

        if this_user.is_anonymous():
            if self.allow_anonymous_view:
                return True
        else:
            if this_user in (self.creator, self.owner):
                return True
            elif self.allow_user_view:
                return True
            elif has_perm(this_user, 'memberships.view_appentry', self):
                return True

        return False

    @property
    def name(self):
        """Get full name"""
        name = '%s %s' % (self.first_name, self.last_name)
        return name.strip()

    @property
    def first_name(self):
        return self.get_field_value('first-name')

    @property
    def last_name(self):
        return self.get_field_value('last-name')

    @property
    def email(self):
        """Get email string"""
        return self.get_field_value('email')

    def approval_required(self):
        """
        Returns a boolean value on whether approval is required
        This is dependent on whether membership is a join or renewal.
        """
        if self.is_renewal:
            return self.membership_type.renewal_require_approval
        else:
            return self.membership_type.require_approval

    @property
    def corporate_membership_id(self):
        """Get corporate_membership_id """
        corporate_membership_id = self.get_field_value('corporate_membership_id')
        try:
            corporate_membership_id = int(corporate_membership_id)
        except:
            corporate_membership_id = 0
        return corporate_membership_id

    def get_field_value(self, field_type):
        try:
            entry_field = self.fields.get(field__field_type=field_type)
            return entry_field.value
        except:
            return unicode()

    @models.permalink
    def hash_url(self):
        return ('membership.application_confirmation', (self.hash,))

    @property
    @models.permalink
    def confirmation_url(self):
        return ('membership.application_confirmation', (self.hash,))

    @property
    def membership_type(self):
        """Get MembershipType object"""

        # Get membership type via name
        try:
            entry_field = self.fields.get(field__field_type="membership-type")
            return MembershipType.objects.get(name__exact=entry_field.value.strip())
        except:
            pass

        # Find an older "approved" membership entry ------------
        if self.user:
            entries = AppEntry.objects.filter(
                user=self.user,
                membership__isnull=False,
                create_dt__lt=self.create_dt,
            ).order_by('-create_dt')

            if entries:
                return entries[0].membership.membership_type

        # If the application only has one membership type choice ,use that ------
        membership_types = self.app.membership_types.all()

        if membership_types.count() == 1:
            return membership_types[0]

        # else return none; boom.

    @property
    def payment_method(self):
        """Get PaymentMethod object"""

        # TODO: don't like this; would prefer object column in field_entry
        # TODO: Prone to error; We're depending on a string membership type name
        try:
            entry_field = self.fields.get(field__field_type="payment-method")
            return PaymentMethod.objects.get(human_name__exact=entry_field.value.strip())
        except PaymentMethod.MultipleObjectsReturned:
            return PaymentMethod.objects.filter(
                human_name__exact=entry_field.value.strip()
            )[0]
        except AppFieldEntry.DoesNotExist, PaymentMethod.DoesNotExist:
            pass

        # Find an older "approved" membership entry ------------
        if self.user:
            entries = AppEntry.objects.filter(
                user=self.user,
                membership__isnull=False,
                create_dt__lt=self.create_dt,
            ).order_by('-create_dt')

            if entries:
                return entries[0].membership.payment_method

        # If the application only has one membership type choice ,use that ------
        payment_methods = self.app.payment_methods.all()

        if payment_methods:
            return payment_methods[0]

        # else return none; boom.

    def applicant(self):
        """Get User object"""
        if self.membership:
            return self.membership.user

    def get_expire_dt(self):
        """
        Get the expiration date.
        Consider their corporate membership.

        Members under a corporate membership expire
        when their corporate membership expires.
        """
        from tendenci.addons.corporate_memberships.models import CorporateMembership

        expire_dt = None
        if self.corporate_membership_id:
            try:
                expire_dt = CorporateMembership.objects.get(pk=self.corporate_membership_id).expiration_dt
            except CorporateMembership.DoesNotExist:
                pass

        if not expire_dt:  # membership record not found; new membership
            expire_dt = self.membership_type.get_expiration_dt(join_dt=datetime.now())

        return expire_dt

    def get_or_create_user(self):
        """
        Return a user that's newly created or already existed.
        """
        created = False

        # get user -------------
        if self.user:
            user = self.user
        # elif self.suggested_users():
        #     user_pk, user_label = self.suggested_users()[0]
        #     user = User.objects.get(pk=user_pk)
        else:
            created = True
            user = User.objects.create_user(**{
                'username': self.spawn_username(self.first_name[0], self.last_name),
                'email': self.email,
                'password': hashlib.sha1(self.email).hexdigest()[:6]
            })

        return user, created

    def approve(self):
        """
        - Bind membership with user
            1. authenticated user
            2. suggestions per fn, ln, email
            3. create new user
        - Update user with membership data (fn, ln, email)
        - Bind user with group
        - Update memberhsip status_detail='active'
        - Update decision_dt=datetime.now()

        More than 1 [active] membership of the same type cannot exist
        """

        # get user -------------
        user, created = self.get_or_create_user()

        user.first_name = self.first_name
        user.last_name = self.last_name
        user.email = self.email
        user.save()

        # get judge --------------
        if self.judge and self.judge.is_authenticated():
            judge, judge_pk, judge_username = self.judge, self.judge.pk, self.judge.username
        else:
            judge, judge_pk, judge_username = None, int(), unicode()

        # update old membership [of same type] -----------
        user.memberships.filter(
            membership_type=self.membership_type,
            status=True,
            status_detail='active'
        ).update(status_detail='archive')

        # look for previous member number
        memberships = user.memberships.order_by('-pk')
        if memberships:
            member_number = memberships[0].member_number
        else:
            # all of this to get the largest membership number
            newest_membership = Membership.objects.order_by('-pk')
            if newest_membership:
                member_number = newest_membership[0].pk + 1000
            else:
                member_number = 1000

        membership = Membership.objects.create(**{
            'member_number': member_number,
            'membership_type': self.membership_type,
            'user': user,
            'renewal': self.membership_type.renewal,
            'subscribe_dt': datetime.now(),
            'expire_dt': self.get_expire_dt(),
            'payment_method': self.payment_method,
            'ma': self.app,
            'corporate_membership_id': self.corporate_membership_id,
            'creator': user,
            'creator_username': user.username,
            'owner': user,
            'owner_username': user.username,
        })

        # populate the member number to profile
        membership.populate_user_member_id()

        try:
            # add user to group
            GroupMembership.objects.create(**{
                'group': self.membership_type.group,
                'member': user,
                'creator_id': judge_pk or user.pk,
                'creator_username': judge_username,
                'owner_id': judge_pk or user.pk,
                'owner_username': judge_username,
                'status': True,
                'status_detail': 'active',
            })
        except:
            pass

        # add user to the groups they checked
        field_entries = self.fields.all()
        for field_entry in field_entries:
            value = field_entry.value
            if field_entry.field.field_function == "Group" and value:
                for val in field_entry.field.function_params.split(','):
                    group = Group.objects.get(name=val)
                    try:
                        GroupMembership.objects.create(**{
                                    'group': group,
                                    'member': user,
                                    'creator_id': judge_pk or user.pk,
                                    'creator_username': judge_username,
                                    'owner_id': judge_pk or user.pk,
                                    'owner_username': judge_username,
                                    'status': True,
                                    'status_detail': 'active',
                                })
                    except:
                        pass

        #Update invoice
        if self.invoice:
            self.invoice.bill_to_first_name = self.user.first_name
            self.invoice.bill_to_last_name = self.user.last_name
            self.invoice.owner = self.user
            self.invoice.owner_username = self.user.username
            self.invoice.save()

        self.is_approved = True
        self.decision_dt = membership.create_dt
        self.membership = membership
        self.save()

    def disapprove(self):

        if self.judge and self.judge.is_authenticated():
            judge, judge_pk, judge_username = self.judge, self.judge.pk, self.judge.username
        else:
            judge, judge_pk, judge_username = None, 0, ''

        self.is_approved = False
        self.decision_dt = datetime.now()
        self.judge = judge
        self.save()

    def suggested_users(self, **kwargs):
        """
        Return list of users.
        List of users is created via fn, ln, and email passed.
        """
        from operator import __or__ as OR

        kwargs = kwargs or {
                # 'first_name': self.first_name,
                # 'last_name': self.last_name,
                'email': self.email
            }
            
        users = {}
        lst = []
        for i in kwargs.items():
            key, value = i
            if value:
                lst.append(Q(i))

        if lst:        
            for u in User.objects.filter(reduce(OR, lst)):
                users[u.pk] = ' '.join([u.first_name, u.last_name, u.username, u.email])

        return users.items()

    def spawn_username(self, *args, **kwargs):
        """
        Join arguments to create username [string].
        Find similiar usernames; auto-increment newest username.
        Return new username [string].
        """
        if not args:
            raise Exception('spawn_username() requires atleast 1 argument; 0 were given')

        max_length = kwargs.get(u'max_length', 9)
        delimiter = kwargs.get(u'delimiter', u'')

        un = ' '.join(args)  # concat args into one string
        un = re.sub('\s+', delimiter, un)  # replace spaces w/ delimiter (default: no-space)
        un = re.sub('[^\w.-]+', u'', un)  # remove non-word-characters
        un = un.strip('_.- ')  # strip funny-characters from sides
        un = un[:max_length].lower()  # keep max length and lowercase username

        others = []  # find similiar usernames
        for u in User.objects.filter(username__startswith=un):
            if u.username.replace(un, '0').isdigit():
                others.append(int(u.username.replace(un, '0')))

        if others and 0 in others:
            # the appended digit will compromise the username length
            # there would have to be more than 99,999 duplicate usernames
            # to kill the database username max field length
            un = '%s%s' % (un, unicode(max(others) + 1))

        return un.lower()

    @property
    def status_msg(self):
        status = 'Pending'

        if self.is_approved:  # property
            status = 'Approved'
        elif self.is_disapproved():  # method
            status = 'Disapproved'

        return status

    def is_pending(self):
        return self.is_approved == None

    def is_disapproved(self):
        return self.is_approved == False

    def make_acct_entries(self, user, inv, amount, **kwargs):
        """
        Make the accounting entries for the event sale
        """
        from tendenci.apps.accountings.models import Acct, AcctEntry, AcctTran
        from tendenci.apps.accountings.utils import make_acct_entries_initial, make_acct_entries_closing

        ae = AcctEntry.objects.create_acct_entry(user, 'invoice', inv.id)
        if not inv.is_tendered:
            make_acct_entries_initial(user, ae, amount)
        else:
            # payment has now been received
            make_acct_entries_closing(user, ae, amount)

            # CREDIT event SALES
            acct_number = self.get_acct_number()
            acct = Acct.objects.get(account_number=acct_number)
            AcctTran.objects.create_acct_tran(user, ae, acct, amount * (-1))

    # to lookup for the number, go to /accountings/account_numbers/
    def get_acct_number(self, discount=False):
        if discount:
            return 462000
        else:
            return 402000

    def auto_update_paid_object(self, request, payment):
        """
        Update the object after online payment is received.
        If auto-approve; approve entry; send emails; log.
        """
        from tendenci.apps.notifications.utils import send_welcome_email

        if self.is_renewal:
            # if auto-approve renews
            if not self.membership_type.renewal_require_approval:
                self.user, created = self.get_or_create_user()
                if created:
                    send_welcome_email(self.user)
                self.approve()
        else:
            # if auto-approve joins
            if not self.membership_type.require_approval:
                self.user, created = self.get_or_create_user()
                if created:
                    send_welcome_email(self.user)
                self.approve()

        if self.is_approved:

            # silence old memberships within renewal period
            Membership.objects.silence_old_memberships(self.user)

            # send "approved" notification
            Notice.send_notice(
                request=request,
                emails=self.email,
                notice_type='approve',
                membership=self.membership,
                membership_type=self.membership_type,
            )

            # log entry approval
            EventLog.objects.log(**{
                'event_id': 1082101,
                'event_data': '%s (%d) approved by %s' % (self._meta.object_name, self.pk, self.judge),
                'description': '%s viewed' % self._meta.object_name,
                'user': request.user,
                'request': request,
                'instance': self,
            })

    def save_invoice(self, **kwargs):
        status_detail = kwargs.get('status_detail', 'tendered')

        content_type = ContentType.objects.get(app_label=self._meta.app_label,
              model=self._meta.module_name)

        try:  # get invoice
            invoice = Invoice.objects.get(
                object_type=content_type,
                object_id=self.pk,
            )
        except:  # else; create invoice
            invoice = Invoice()
            invoice.object_type = content_type
            invoice.object_id = self.pk

        # update invoice with details
        invoice.estimate = True
        invoice.status_detail = status_detail

        invoice.bill_to = '%s %s' % (self.first_name, self.last_name)
        invoice.bill_to_first_name = self.first_name
        invoice.bill_to_last_name = self.last_name
        invoice.bill_to_email = self.email

        # if this membership is under a corporate and its corporate membership allows
        # threshold and the threshold is whithin limit, then this membership gets the
        # threshold price.

        (use_threshold, threshold_price) = self.get_corp_memb_threshold_price()
        membership_price = self.get_memb_price()
                
        if use_threshold:
            invoice.subtotal = threshold_price
            invoice.total = threshold_price
            invoice.balance = threshold_price
        else:
            invoice.subtotal = membership_price
            invoice.total = membership_price
            invoice.balance = membership_price

        invoice.due_date = datetime.now()  # TODO: change model field to null=True
        invoice.ship_date = datetime.now()  # TODO: change model field to null=True

        invoice.save()

        self.invoice = invoice
        self.save()

        return invoice

    def get_memb_price(self):
        membership_price = self.membership_type.price
        if self.membership_type.admin_fee:
            membership_price = self.membership_type.price + self.membership_type.admin_fee
        return membership_price

    def get_corp_memb_threshold_price(self):
        """
        get the threshold price for this individual.
        return tuple (use_threshold, threshold_price)
        """
        from tendenci.addons.corporate_memberships.models import CorporateMembership
        try:
            corp_memb = CorporateMembership.objects.get(id=self.corporate_membership_id)
        except CorporateMembership.DoesNotExist:
            corp_memb = None

        if corp_memb:
            allow_threshold = corp_memb.corporate_membership_type.apply_threshold
            threshold_limit = corp_memb.corporate_membership_type.individual_threshold
            threshold_price = corp_memb.corporate_membership_type.individual_threshold_price
            if self.membership_type.admin_fee:
                threshold_price = corp_memb.corporate_membership_type.individual_threshold_price + self.membership_type.admin_fee
                
            if allow_threshold and threshold_limit and threshold_limit > 0:
                # check how many memberships have joined under this corporate
                field_entries = AppFieldEntry.objects.filter(
                    field__field_type='corporate_membership_id',
                    value=corp_memb.id
                )
                count = field_entries.count()
                if count <= threshold_limit:
                    return True, threshold_price

        return False, None
        
    def execute_field_functions(self):
        app = self.app
        fields = app.fields.exclude(field_function=None)
        for field in fields:
            field.execute_function(self)

    @property
    def items(self):
        """
        Returns a dictionary of entry fields.
        """
        return self.get_items()

    def get_items(self, slugify_label=True):
        items = {}
        entry = self

        if entry:
            for field in entry.fields.all():
                label = field.field.label
                if slugify_label:
                    label = slugify(label).replace('-', '_')
                items[label] = field.value

        return items

    def ordered_fields(self):
        return self.fields.all().order_by('field__position')


class AppFieldEntry(models.Model):
    """
    A single field value for a form entry submitted via a membership application.
    """
    entry = models.ForeignKey("AppEntry", related_name="fields")
    field = models.ForeignKey("AppField", related_name="field")
    value = models.CharField(max_length=2000)

    def __unicode__(self):
        return "%s %s" % (self.field.label, self.value)

    class Meta:
        verbose_name = _("Application Field Entry")
        verbose_name_plural = _("Application Field Entries")

    def corporate_membership_name(self):
        if self.field.field_type == 'corporate_membership_id':
            try:
                #from tendenci.addons.corporate_memberships.models import CorporateMembership
                from django.db import connection
                cursor = connection.cursor()
                cursor.execute("""
                    SELECT name
                    FROM corporate_memberships_corporatemembership
                    WHERE id=%d
                    LIMIT 1 """ % int(self.value))
                rows = cursor.fetchall()
                if rows:
                    return rows[0][0]
            except:
                pass

        return None


# Moved from management/__init__.py to here because it breaks
# the management commands due to the ImportError.
# assign models permissions to the admin auth group
def assign_permissions(app, created_models, verbosity, **kwargs):
    from tendenci.core.perms.utils import update_admin_group_perms
    update_admin_group_perms()
from django.db.models.signals import post_syncdb
#from memberships import models as membership_models
post_syncdb.connect(assign_permissions, sender=__file__)
