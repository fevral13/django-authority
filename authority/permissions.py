from django.conf import settings
from django.contrib.auth.models import Permission as DjangoPermission
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.base import Model, ModelBase
from django.template.defaultfilters import slugify

from authority.exceptions import NotAModel, UnsavedModelInstance
from authority.models import Permission


class PermissionMetaclass(type):
    """
    Used to generate the default set of permission checks "add", "change" and
    "delete".
    """
    def __new__(cls, name, bases, attrs):
        new_class = super(
            PermissionMetaclass, cls).__new__(cls, name, bases, attrs)
        if not new_class.label:
            new_class.label = "%s_permission" % new_class.__name__.lower()
        new_class.label = slugify(new_class.label)
        if new_class.checks is None:
            new_class.checks = []
        # force check names to be lower case
        new_class.checks = [check.lower() for check in new_class.checks]
        return new_class


class BasePermission(object):
    """
    Base Permission class to be used to define app permissions.
    """
    __metaclass__ = PermissionMetaclass

    checks = ()
    label = None
    generic_checks = ['add', 'browse', 'change', 'delete']

    def __init__(self, user=None, group=None, *args, **kwargs):
        self.user = user
        self.group = group
        super(BasePermission, self).__init__(*args, **kwargs)

    def _get_cached_perms(self):
        """
        Set up both the user and group caches.
        """
        if not self.user:
            return {}, {}
        perms = Permission.objects.filter(
            Q(user__pk=self.user.pk) | Q(group__in=self.user.groups.all()),
        )
        user_permissions = {}
        group_permissions = {}
        for perm in perms:
            if perm.user_id == self.user.pk:
                user_permissions[(
                    perm.object_id,
                    perm.content_type_id,
                    perm.codename,
                    perm.approved,
                )] = True
            # If the user has the permission do for something, but perm.user !=
            # self.user then by definition that permission came from the
            # group.
            else:
                group_permissions[(
                    perm.object_id,
                    perm.content_type_id,
                    perm.codename,
                    perm.approved,
                )] = True
        return user_permissions, group_permissions

    def _prime_perm_caches(self):
        """
        Prime both the user and group caches and put them on the ``self.user``.
        In addition add a cache filled flag on ``self.user``.
        """
        perm_cache, group_perm_cache = self._get_cached_perms()
        self.user._authority_perm_cache = perm_cache
        self.user._authority_group_perm_cache = group_perm_cache
        self.user._authority_perm_cache_filled = True

    @property
    def _perm_cache(self):
        """
        cached_permissions will generate the cache in a lazy fashion.
        """
        # Check to see if the cache has been primed.
        if not self.user:
            return {}
        cache_filled = getattr(
            self.user,
            '_authority_perm_cache_filled',
            False,
        )
        if cache_filled:
            # Don't really like the name for this, but this matches how Django
            # does it.
            return self.user._authority_perm_cache

        # Prime the cache.
        self._prime_perm_caches()
        return self.user._authority_perm_cache

    @property
    def _group_perm_cache(self):
        """
        cached_permissions will generate the cache in a lazy fashion.
        """
        # Check to see if the cache has been primed.
        if not self.user:
            return {}
        cache_filled = getattr(
            self.user,
            '_authority_perm_cache_filled',
            False,
        )
        if cache_filled:
            return self.user._authority_group_perm_cache

        # Prime the cache.
        self._prime_perm_caches()
        return self.user._authority_group_perm_cache

    def invalidate_permissions_cache(self):
        """
        In the event that the Permission table is changed during the use of a
        permission the Permission cache will need to be invalidated and
        regenerated. By calling this method the invalidation will occur, and
        the next time the cached_permissions is used the cache will be
        re-primed.
        """
        if self.user:
            self.user._authority_perm_cache_filled = False

    @property
    def use_smart_cache(self):
        # AUTHORITY_USE_SMART_CACHE defaults to False to maintain backwards
        # compatibility.
        use_smart_cache = getattr(settings, 'AUTHORITY_USE_SMART_CACHE', True)
        return self.user and use_smart_cache

    def has_user_perms(self, perm, obj, approved, check_groups=True):
        if not self.user:
            return False
        if self.user.is_superuser:
            return True
        if not self.user.is_active:
            return False

        if self.use_smart_cache:
            content_type_pk = Permission.objects.get_content_type(obj).pk

            def _user_has_perms(cached_perms):
                # Check to see if the permission is in the cache.
                return cached_perms.get((
                    obj.pk,
                    content_type_pk,
                    perm,
                    approved,
                ))

            # Check to see if the permission is in the cache.
            has_perm = _user_has_perms(self._perm_cache)

            # Optionally check group permissions
            if check_groups:
                has_perm = has_perm or _user_has_perms(self._group_perm_cache)
            return has_perm
        else:
            return Permission.objects.user_permissions(
                self.user,
                perm,
                obj,
                approved,
                check_groups,
            ).filter(
                object_id=obj.pk,
            ).exists()

        return False

    def has_group_perms(self, perm, obj, approved):
        """
        Check if group has the permission for the given object
        """
        if self.group:
            perms = Permission.objects.group_permissions(self.group, perm, obj,
                                                         approved)
            return perms.filter(object_id=obj.pk)
        return False

    def has_perm(self, perm, obj, check_groups=True, approved=True):
        """
        Check if user has the permission for the given object
        """
        if self.user:
            if self.has_user_perms(perm, obj, approved, check_groups):
                return True
        if self.group:
            return self.has_group_perms(perm, obj, approved)
        return False

    def requested_perm(self, perm, obj, check_groups=True):
        """
        Check if user requested a permission for the given object
        """
        return self.has_perm(perm, obj, check_groups, False)

    def can(self, check, generic=False, *args, **kwargs):
        if not args:
            args = [self.model]
        perms = False
        for obj in args:
            # skip this obj if it's not a model class or instance
            if not isinstance(obj, (ModelBase, Model)):
                continue
            # first check Django's permission system
            if self.user:
                perm = self.get_django_codename(check, obj, generic)
                perms = perms or self.user.has_perm(perm)
            perm = self.get_codename(check, obj, generic)
            # then check authority's per object permissions
            if not isinstance(obj, ModelBase) and isinstance(obj, self.model):
                # only check the authority if obj is not a model class
                perms = perms or self.has_perm(perm, obj)
        return perms

    def get_django_codename(
            self, check, model_or_instance, generic=False, without_left=False):
        if without_left:
            perm = check
        else:
            perm = '%s.%s' % (model_or_instance._meta.app_label, check.lower())
        if generic:
            perm = '%s_%s' % (
                perm,
                model_or_instance._meta.object_name.lower(),
            )
        return perm

    def get_codename(self, check, model_or_instance, generic=False):
        perm = '%s.%s' % (self.label, check.lower())
        if generic:
            perm = '%s_%s' % (
                perm,
                model_or_instance._meta.object_name.lower(),
            )
        return perm

    def assign(self, check=None, content_object=None, generic=False):
        """
        Assign a permission to a user.

        To assign permission for all checks: let check=None.
        To assign permission for all objects: let content_object=None.

        If generic is True then "check" will be suffixed with _modelname.
        """
        result = []

        if not content_object:
            content_objects = (self.model,)
        elif not isinstance(content_object, (list, tuple)):
            content_objects = (content_object,)
        else:
            content_objects = content_object

        if not check:
            checks = self.generic_checks + getattr(self, 'checks', [])
        elif not isinstance(check, (list, tuple)):
            checks = (check,)
        else:
            checks = check

        for content_object in content_objects:
            # raise an exception before adding any permission
            # i think Django does not rollback by default
            if not isinstance(content_object, (Model, ModelBase)):
                raise NotAModel(content_object)
            elif isinstance(content_object, Model) and not content_object.pk:
                raise UnsavedModelInstance(content_object)

            content_type = ContentType.objects.get_for_model(content_object)

            for check in checks:
                if isinstance(content_object, Model):
                    # make an authority per object permission
                    codename = self.get_codename(
                        check,
                        content_object,
                        generic,
                    )
                    try:
                        perm = Permission.objects.get(
                            user=self.user,
                            codename=codename,
                            approved=True,
                            content_type=content_type,
                            object_id=content_object.pk,
                        )
                    except Permission.DoesNotExist:
                        perm = Permission.objects.create(
                            user=self.user,
                            content_object=content_object,
                            codename=codename,
                            approved=True,
                        )

                    result.append(perm)

                elif isinstance(content_object, ModelBase):
                    # make a Django permission
                    codename = self.get_django_codename(
                        check,
                        content_object,
                        generic,
                        without_left=True,
                    )
                    try:
                        perm = DjangoPermission.objects.get(codename=codename)
                    except DjangoPermission.DoesNotExist:
                        name = check
                        if '_' in name:
                            name = name[0:name.find('_')]
                        perm = DjangoPermission(
                            name=name,
                            codename=codename,
                            content_type=content_type,
                        )
                        perm.save()
                    self.user.user_permissions.add(perm)
                    result.append(perm)

        return result
