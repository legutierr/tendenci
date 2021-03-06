import os
import re
import random
import string
import zipfile
import shutil

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from createsend import CreateSend, Client, Subscriber
from createsend.createsend import BadRequest

from tendenci.addons.campaign_monitor.models import Campaign, Template


api_key = getattr(settings, 'CAMPAIGNMONITOR_API_KEY', None)
api_password = getattr(settings, 'CAMPAIGNMONITOR_API_PASSWORD', None)
client_id = getattr(settings, 'CAMPAIGNMONITOR_API_CLIENT_ID', None)
CreateSend.api_key = api_key
cl = Client(client_id)

def random_string(n=32):
    return ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for x in range(n))
    
def temporary_id():
    exists = True
    while(exists):
        id = random_string()
        if not Template.objects.filter(template_id=id).exists():
            exists = False
    return id

def sync_campaigns():
    sent = cl.campaigns()
    for c in sent:
        try:
            campaign = Campaign.objects.get(campaign_id = c.CampaignID)
        except Campaign.DoesNotExist:
            campaign = Campaign(campaign_id = c.CampaignID)
        campaign.subject = c.Subject
        campaign.name = c.Name
        campaign.sent_date = c.SentDate
        campaign.web_version_url = c.WebVersionURL
        campaign.total_recipients = c.TotalRecipients
        campaign.status = 'S' #sent
        campaign.save()
    
    if hasattr(cl,'scheduled'): scheduled = cl.scheduled()
    else: scheduled = []
    for c in scheduled:
        try:
            campaign = Campaign.objects.get(campaign_id = c.CampaignID)
        except Campaign.DoesNotExist:
            campaign = Campaign(campaign_id = c.CampaignID)
        campaign.subject = c.Subject
        campaign.name = c.Name
        campaign.scheduled_date = c.DateScheduled
        campaign.scheduled_time_zone = c.ScheduledTimeZone
        campaign.preview_url = c.PreviewURL
        campaign.status = 'C' #Scheduled
        campaign.save()
    
    if hasattr(cl,'drafts'): drafts = cl.drafts()
    else: drafts = []
    for c in drafts:
        try:
            campaign = Campaign.objects.get(campaign_id = c.CampaignID)
        except Campaign.DoesNotExist:
            campaign = Campaign(campaign_id = c.CampaignID)
        campaign.subject = c.Subject
        campaign.name = c.Name
        campaign.preview_url = c.PreviewURL
        campaign.save()

def sync_templates():
    if hasattr(cl,'templates'): templates = cl.templates()
    else: templates = []
    for t in templates:
        try:
            template = Template.objects.get(template_id = t.TemplateID)
        except Template.DoesNotExist:
            template = Template(template_id = t.TemplateID)
        template.name = t.Name
        template.cm_preview_url = t.PreviewURL
        template.cm_screenshot_url = t.ScreenshotURL
        template.save()


def extract_files(template):
    if template.zip_file:
        zip_file = zipfile.ZipFile(template.zip_file.file)
        if hasattr(settings, 'USE_S3_STORAGE') and settings.USE_S3_STORAGE:
            # create a tmp directory to extract the zip file
            tmp_dir = 'tmp_%d' % template.id
            path = './%s/campaign_monitor/%s' % (tmp_dir, template.template_id)
            zip_file.extractall(path)
            # upload extracted files to s3
            for root, dirs, files in os.walk(path):
                for name in files:
                    file_path = os.path.join(root, name)
                    dst_file_path = file_path.replace('./%s/' % tmp_dir, '')
                    default_storage.save(dst_file_path,
                                ContentFile(open(file_path).read()))

            # remove the tmp directory
            shutil.rmtree(tmp_dir)
        else:
            path = os.path.join(settings.MEDIA_ROOT,
                                'campaign_monitor',
                                template.template_id)
            zip_file.extractall(path)


def apply_template_media(template):
    """
    Prepends files in content to the media path
    of a given template's zip file contents
    """
    content = unicode(template.html_file.file.read(), "utf-8")
    pattern = r'"[^"]*?\.(?:(?i)jpg|(?i)jpeg|(?i)png|(?i)gif|(?i)bmp|(?i)tif|(?i)css)"'
    repl = lambda x: '"%s/%s"' % (template.get_media_url(), x.group(0).replace('"', ''))
    new_content = re.sub(pattern, repl, content)
    return new_content

def update_subscription(profile, old_email):
    """
    Update a profile subscription on campaign monitor.
    If the old email is not on CM this will not subscribe the user
    to CM.
    """
    if api_key and client_id:
        user = profile.user
        for group in profile.get_groups():
            for listmap in group.listmap_set.all():
                subscriber = Subscriber(listmap.list_id, old_email)
                try:
                    subscriber.update(user.email, user.get_full_name(), [], False)
                except BadRequest, e:
                    print e
            
