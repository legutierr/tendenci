from haystack import indexes

from django.db.models import signals

from tendenci.core.perms.object_perms import ObjectPermission
from tendenci.apps.search.indexes import CustomSearchIndex


class TendenciBaseSearchIndex(CustomSearchIndex):
    text = indexes.CharField(document=True, use_template=True)
    
    # TendenciBaseModel Fields
    allow_anonymous_view = indexes.BooleanField(model_attr='allow_anonymous_view')
    allow_user_view = indexes.BooleanField(model_attr='allow_user_view')
    allow_member_view = indexes.BooleanField(model_attr='allow_member_view')
    # allow_anonymous_edit = indexes.BooleanField(model_attr='allow_anonymous_edit')
    allow_user_edit = indexes.BooleanField(model_attr='allow_user_edit')
    allow_member_edit = indexes.BooleanField(model_attr='allow_member_edit')
    creator = indexes.CharField(model_attr='creator')
    creator_username = indexes.CharField(model_attr='creator_username')
    owner = indexes.CharField(model_attr='owner')
    owner_username = indexes.CharField(model_attr='owner_username')
    status = indexes.BooleanField(model_attr='status')
    status_detail = indexes.CharField(model_attr='status_detail')
    create_dt = indexes.DateTimeField(model_attr='create_dt', null=True)
    update_dt = indexes.DateTimeField(model_attr='update_dt', null=True)

    # permission fields
    users_can_view = indexes.MultiValueField(null=True)
    groups_can_view = indexes.MultiValueField(null=True)
    
    # PK: needed for exclude list_tags
    primary_key = indexes.CharField(model_attr='pk')

    def get_updated_field(self):
        return 'update_dt'

    def prepare_users_can_view(self, obj):
        """
        This needs to be overwritten if 'view' permission label does not follow the standard convention:
        (app_label).view_(module_name)
        """
        return ObjectPermission.objects.users_with_perms('%s.view_%s' % (obj._meta.app_label, obj._meta.module_name), obj)

    def prepare_groups_can_view(self, obj):
        """
        This needs to be overwritten if 'view' permission label does not follow the standard convention:
        (app_label).view_(module_name)
        """
        return ObjectPermission.objects.groups_with_perms('%s.view_%s' % (obj._meta.app_label, obj._meta.module_name), obj)

