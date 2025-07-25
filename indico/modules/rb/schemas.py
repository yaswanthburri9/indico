# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from operator import itemgetter

from babel.dates import get_timezone
from flask import session
from marshmallow import ValidationError, fields, post_dump, post_load, validate, validates, validates_schema
from marshmallow.fields import Boolean, Date, DateTime, Function, Method, Nested, Number, Pluck, String
from sqlalchemy import func

from indico.core.config import config
from indico.core.db.sqlalchemy.links import LinkType
from indico.core.db.sqlalchemy.protection import ProtectionMode
from indico.core.marshmallow import mm
from indico.modules.categories.models.categories import Category
from indico.modules.events.sessions.models.blocks import SessionBlock
from indico.modules.rb import BookingReasonRequiredOptions, rb_settings
from indico.modules.rb.models.blocked_rooms import BlockedRoom, BlockedRoomState
from indico.modules.rb.models.blockings import Blocking
from indico.modules.rb.models.equipment import EquipmentType
from indico.modules.rb.models.locations import Location
from indico.modules.rb.models.map_areas import MapArea
from indico.modules.rb.models.principals import LocationPrincipal, RoomPrincipal
from indico.modules.rb.models.reservation_edit_logs import ReservationEditLog
from indico.modules.rb.models.reservation_occurrences import (ReservationOccurrence, ReservationOccurrenceLink,
                                                              ReservationOccurrenceState)
from indico.modules.rb.models.reservations import RepeatFrequency, Reservation, ReservationState
from indico.modules.rb.models.room_attributes import RoomAttribute, RoomAttributeAssociation
from indico.modules.rb.models.room_bookable_hours import BookableHours
from indico.modules.rb.models.room_features import RoomFeature
from indico.modules.rb.models.room_nonbookable_periods import NonBookablePeriod
from indico.modules.rb.models.rooms import Room
from indico.modules.rb.util import WEEKDAYS, rb_is_admin
from indico.modules.users.schemas import UserSchema
from indico.util.i18n import _
from indico.util.marshmallow import (ModelList, NaiveDateTime, Principal, PrincipalList, PrincipalPermissionList,
                                     not_empty, validate_format_placeholders)
from indico.util.string import natural_sort_key


class RoomAttributeValuesSchema(mm.SQLAlchemyAutoSchema):
    title = String(attribute='attribute.title')
    name = String(attribute='attribute.name')

    class Meta:
        model = RoomAttributeAssociation
        fields = ('value', 'title', 'name')


class AttributesSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = RoomAttribute
        fields = ('name', 'title', 'is_required', 'is_hidden')


class RoomSchema(mm.SQLAlchemyAutoSchema):
    owner_name = String(attribute='owner.full_name')

    class Meta:
        model = Room
        fields = ('id', 'name', 'capacity', 'building', 'floor', 'number', 'is_public', 'location_name', 'full_name',
                  'comments', 'division', 'is_reservable', 'reservations_need_confirmation', 'sprite_position',
                  'surface_area', 'latitude', 'longitude', 'telephone', 'key_location', 'max_advance_days',
                  'owner_name', 'available_equipment', 'has_photo', 'verbose_name', 'map_url', 'site', 'location_id')


class AdminRoomSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        modal = Room
        fields = ('id', 'location_id', 'name', 'full_name', 'sprite_position', 'owner_name', 'comments')


class RoomUpdateSchema(RoomSchema):
    owner = Principal()
    acl_entries = PrincipalPermissionList(RoomPrincipal)
    protection_mode = fields.Enum(ProtectionMode)

    class Meta(RoomSchema.Meta):
        fields = (*RoomSchema.Meta.fields, 'notification_before_days', 'notification_before_days_weekly', 'owner',
                  'notification_before_days_monthly', 'notifications_enabled', 'end_notification_daily',
                  'end_notification_weekly', 'end_notification_monthly', 'end_notifications_enabled', 'verbose_name',
                  'site', 'notification_emails', 'booking_limit_days', 'acl_entries', 'protection_mode')


class RoomUpdateArgsSchema(mm.Schema):
    verbose_name = fields.String(allow_none=True)
    site = fields.String(allow_none=True)
    building = fields.String(validate=lambda x: x is not None)
    floor = fields.String(validate=lambda x: x is not None)
    number = fields.String(validate=lambda x: x is not None)
    longitude = fields.Float(allow_none=True)
    latitude = fields.Float(allow_none=True)
    is_reservable = fields.Boolean(allow_none=True)
    reservations_need_confirmation = fields.Boolean(allow_none=True)
    notification_emails = fields.List(fields.Email())
    notification_before_days = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    notification_before_days_weekly = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    notification_before_days_monthly = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    notifications_enabled = fields.Boolean()
    end_notification_daily = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    end_notification_weekly = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    end_notification_monthly = fields.Int(validate=lambda x: 1 <= x <= 30, allow_none=True)
    end_notifications_enabled = fields.Boolean()
    booking_limit_days = fields.Int(validate=lambda x: x >= 1, allow_none=True)
    owner = Principal(validate=lambda x: x is not None, allow_none=True)
    key_location = fields.String()
    telephone = fields.String()
    capacity = fields.Int(validate=lambda x: x >= 1)
    division = fields.String(allow_none=True)
    surface_area = fields.Int(validate=lambda x: x >= 0, allow_none=True)
    max_advance_days = fields.Int(validate=lambda x: x >= 1, allow_none=True)
    comments = fields.String()
    acl_entries = PrincipalPermissionList(RoomPrincipal)
    protection_mode = fields.Enum(ProtectionMode)


class RoomEquipmentSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = Room
        fields = ('available_equipment',)


class MapAreaSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = MapArea
        fields = ('name', 'top_left_latitude', 'top_left_longitude', 'bottom_right_latitude', 'bottom_right_longitude',
                  'is_default', 'id')


class ReservationSchema(mm.SQLAlchemyAutoSchema):
    start_dt = NaiveDateTime()
    end_dt = NaiveDateTime()

    class Meta:
        model = Reservation
        fields = ('id', 'booking_reason', 'booked_for_name', 'room_id', 'is_accepted', 'start_dt', 'end_dt',
                  'is_repeating', 'repeat_frequency', 'repeat_interval', 'recurrence_weekdays')

    @post_dump(pass_original=True)
    def _hide_sensitive_data(self, data, booking, **kwargs):
        user = session.user if session else None
        if not booking.can_see_details(user):
            data['booked_for_name'] = None
        return data

    @post_dump(pass_original=True)
    def _add_missing_weekdays(self, data, booking, **kwargs):
        if booking.repeat_frequency == RepeatFrequency.WEEK and booking.recurrence_weekdays is None:
            # Weekly booking created before the recurrence weekdays have been implemented
            data['recurrence_weekdays'] = [WEEKDAYS[booking.start_dt.weekday()]]
        return data


class ReservationLinkedObjectDataSchema(mm.Schema):
    id = Number()
    title = Method('_get_title')
    url = Function(lambda obj: obj.url)
    event_title = Function(lambda obj: obj.event.title)
    event_url = Function(lambda obj: obj.event.url)
    own_room_id = Number()
    own_room_name = Function(lambda obj: (obj.own_room.name if obj.own_room else obj.own_room_name) or None)
    start_dt = DateTime()
    end_dt = DateTime()

    def _get_title(self, obj):
        if isinstance(obj, SessionBlock):
            return obj.full_title
        return obj.title


class ReservationUserEventSchema(mm.Schema):
    id = Number()
    title = String()
    url = String()
    start_dt = DateTime()
    end_dt = DateTime()


class ReservationOccurrenceLinkSchema(mm.SQLAlchemyAutoSchema):
    id = Number()
    type = fields.Enum(LinkType, attribute='link_type')
    object = Nested(ReservationLinkedObjectDataSchema,
                    only=('url', 'title', 'event_title', 'event_url', 'start_dt', 'end_dt'))
    start_dt = NaiveDateTime(attribute='reservation_occurrence.start_dt')
    state = fields.Enum(ReservationOccurrenceState, attribute='reservation_occurrence.state')

    @post_dump(pass_original=True)
    def _hide_restricted_object(self, data, link, **kwargs):
        if not link.object.can_access(session.user):
            data['object'] = None
        return data

    class Meta:
        model = ReservationOccurrenceLink
        fields = ('id', 'type', 'object', 'start_dt', 'state')


class ReservationOccurrenceSchema(mm.SQLAlchemyAutoSchema):
    reservation = Nested(ReservationSchema)
    state = fields.Enum(ReservationOccurrenceState)
    start_dt = NaiveDateTime()
    end_dt = NaiveDateTime()

    class Meta:
        model = ReservationOccurrence
        fields = ('start_dt', 'end_dt', 'is_valid', 'reservation', 'rejection_reason', 'state', 'link_id')


class ReservationOccurrenceSchemaWithPermissions(ReservationOccurrenceSchema):
    permissions = Method('_get_permissions')

    class Meta:
        fields = (*ReservationOccurrenceSchema.Meta.fields, 'permissions')

    def _get_permissions(self, occurrence):
        methods = ('can_cancel', 'can_reject')
        admin_permissions = None
        user_permissions = {x: getattr(occurrence, x)(session.user, allow_admin=False) for x in methods}
        if rb_is_admin(session.user):
            admin_permissions = {x: getattr(occurrence, x)(session.user) for x in methods}
        return {'user': user_permissions, 'admin': admin_permissions}


class ReservationConcurrentOccurrenceSchema(ReservationOccurrenceSchema):
    reservations = Nested(ReservationSchema, many=True)

    class Meta:
        fields = (*ReservationOccurrenceSchema.Meta.fields, 'reservations')
        exclude = ('reservation',)


class ReservationEditLogSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = ReservationEditLog
        fields = ('id', 'timestamp', 'info', 'user_name')

    @post_dump(pass_many=True)
    def sort_logs(self, data, many, **kwargs):
        if many:
            data = sorted(data, key=itemgetter('timestamp'), reverse=True)
        return data


class ReservationDetailsSchema(mm.SQLAlchemyAutoSchema):
    booked_for_user = Nested(UserSchema, only=('id', 'identifier', 'full_name', 'phone', 'email'))
    created_by_user = Nested(UserSchema, only=('id', 'identifier', 'full_name', 'email'))
    edit_logs = Nested(ReservationEditLogSchema, many=True)
    can_accept = Function(lambda booking: booking.can_accept(session.user))
    can_cancel = Function(lambda booking: booking.can_cancel(session.user))
    can_delete = Function(lambda booking: booking.can_delete(session.user))
    can_edit = Function(lambda booking: booking.can_edit(session.user))
    can_reject = Function(lambda booking: booking.can_reject(session.user))
    permissions = Method('_get_permissions')
    state = fields.Enum(ReservationState)
    is_linked_to_objects = Function(lambda booking: bool(booking.links))
    start_dt = NaiveDateTime()
    end_dt = NaiveDateTime()

    class Meta:
        model = Reservation
        fields = ('id', 'start_dt', 'end_dt', 'repetition', 'booking_reason', 'created_dt', 'booked_for_user',
                  'room_id', 'created_by_user', 'edit_logs', 'permissions',
                  'is_cancelled', 'is_rejected', 'is_accepted', 'is_pending', 'rejection_reason',
                  'is_linked_to_objects', 'state', 'external_details_url', 'internal_note', 'recurrence_weekdays')

    def _get_permissions(self, booking):
        methods = ('can_accept', 'can_cancel', 'can_delete', 'can_edit', 'can_reject')
        admin_permissions = None
        user_permissions = {x: getattr(booking, x)(session.user, allow_admin=False) for x in methods}
        if rb_is_admin(session.user):
            admin_permissions = {x: getattr(booking, x)(session.user) for x in methods}
        return {'user': user_permissions, 'admin': admin_permissions}

    @post_dump(pass_original=True)
    def _hide_sensitive_data(self, data, booking, **kwargs):
        user = session.user if session else None
        if not booking.room.can_manage(user):
            del data['internal_note']
        if not booking.can_see_details(user):
            data['booked_for_user'] = None
            data['created_by_user'] = None
            data['edit_logs'] = None
        return data

    @post_dump(pass_original=True)
    def _add_missing_weekdays(self, data, booking, **kwargs):
        if booking.repeat_frequency == RepeatFrequency.WEEK and booking.recurrence_weekdays is None:
            # Weekly booking created before the recurrence weekdays have been implemented. In that case
            # we need to get the weekdays from the start date of the booking so the JS code which expects
            # them to be present (e.g. when editing a booking) works properly
            weekdays = [WEEKDAYS[booking.start_dt.weekday()]]
            data['recurrence_weekdays'] = weekdays
            data['repetition'] = (*data['repetition'][:2], weekdays)
        return data


class BlockedRoomSchema(mm.SQLAlchemyAutoSchema):
    room = Nested(RoomSchema, only=('id', 'name', 'sprite_position', 'full_name'))
    state = fields.Enum(BlockedRoomState)

    class Meta:
        model = BlockedRoom
        fields = ('room', 'state', 'rejection_reason', 'rejected_by')

    @post_dump(pass_many=True)
    def sort_rooms(self, data, many, **kwargs):
        if many:
            data = sorted(data, key=lambda x: natural_sort_key(x['room']['full_name']))
        return data


class BlockingSchema(mm.SQLAlchemyAutoSchema):
    blocked_rooms = Nested(BlockedRoomSchema, many=True)
    allowed = PrincipalList()
    permissions = Method('_get_permissions')
    created_by = Pluck(UserSchema, 'full_name', attribute='created_by_user')

    class Meta:
        model = Blocking
        fields = ('id', 'start_date', 'end_date', 'reason', 'blocked_rooms', 'allowed', 'created_by', 'permissions')

    def _get_permissions(self, blocking):
        methods = ('can_delete', 'can_edit')
        admin_permissions = None
        user_permissions = {x: getattr(blocking, x)(session.user, allow_admin=False) for x in methods}
        if rb_is_admin(session.user):
            admin_permissions = {x: getattr(blocking, x)(session.user) for x in methods}
        return {'user': user_permissions, 'admin': admin_permissions}


class NonBookablePeriodSchema(mm.SQLAlchemyAutoSchema):
    start_dt = NaiveDateTime()
    end_dt = NaiveDateTime()

    class Meta:
        model = NonBookablePeriod
        fields = ('start_dt', 'end_dt')


class NonBookablePeriodAdminSchema(mm.SQLAlchemyAutoSchema):
    start_dt = Date()
    end_dt = Date()

    class Meta:
        model = NonBookablePeriod
        fields = ('start_dt', 'end_dt')

    @post_dump(pass_many=True)
    def sort_list(self, data, many, **kwargs):
        if many:
            data = sorted(data, key=itemgetter('start_dt', 'end_dt'))
        return data


class BookableHoursSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = BookableHours
        fields = ('start_time', 'end_time', 'weekday')


class LocationsSchema(mm.SQLAlchemyAutoSchema):
    rooms = Nested(RoomSchema, many=True, only=('id', 'name', 'full_name', 'sprite_position'))

    class Meta:
        model = Location
        fields = ('id', 'name', 'rooms')


class AdminLocationsSchema(mm.SQLAlchemyAutoSchema):
    can_delete = Function(lambda loc: loc.can_delete(session.user) and not loc.rooms)
    acl_entries = PrincipalPermissionList(LocationPrincipal)
    can_edit = Function(lambda loc: loc.can_manage(session.user))

    class Meta:
        model = Location
        fields = ('id', 'name', 'can_edit', 'can_delete', 'map_url_template', 'room_name_format', 'acl_entries')


class RBUserSchema(UserSchema):
    has_owned_rooms = mm.Method('has_managed_rooms')
    has_moderated_rooms = mm.Method('_has_moderated_rooms')
    is_rb_admin = mm.Function(lambda user: rb_is_admin(user))
    is_rb_location_manager = mm.Method('has_managed_locations')

    class Meta:
        fields = (*UserSchema.Meta.fields, 'has_owned_rooms', 'has_moderated_rooms', 'is_admin', 'is_rb_admin',
                  'is_rb_location_manager', 'identifier', 'full_name')

    def has_managed_rooms(self, user):
        from indico.modules.rb.operations.rooms import has_managed_rooms
        return has_managed_rooms(user)

    def _has_moderated_rooms(self, user):
        from indico.modules.rb.operations.rooms import has_managed_rooms
        return has_managed_rooms(user, permission='moderate', explicit=True)

    def has_managed_locations(self, user):
        from indico.modules.rb.operations.locations import has_managed_locations
        return has_managed_locations(user)


class CreateBookingSchema(mm.Schema):
    class Meta:
        rh_context = ('booking',)

    start_dt = fields.DateTime(required=True)
    end_dt = fields.DateTime(required=True)
    repeat_frequency = fields.Enum(RepeatFrequency, required=True)
    repeat_interval = fields.Int(load_default=0, validate=lambda x: x >= 0)
    recurrence_weekdays = fields.List(fields.Str(validate=validate.OneOf(WEEKDAYS)))
    room_id = fields.Int(required=True)
    booked_for_user = Principal(data_key='user', allow_external_users=True)
    booking_reason = fields.String(data_key='reason', load_default='')
    internal_note = fields.String()
    is_prebooking = fields.Bool(load_default=False)
    link_type = fields.Enum(LinkType)
    link_id = fields.Int()
    link_back = fields.Bool(load_default=False)
    admin_override_enabled = fields.Bool(load_default=False)
    extra_fields = fields.Dict(load_default=lambda: {})

    @validates_schema(skip_on_field_errors=True)
    def validate_dts(self, data, **kwargs):
        if data['start_dt'] >= data['end_dt']:
            raise ValidationError(_('Booking cannot end before it starts'))

    @validates('recurrence_weekdays')
    def _check_weekdays_unique(self, weekdays, **kwargs):
        if weekdays and len(weekdays) != len(set(weekdays)):
            raise ValidationError('Duplicate weekdays')

    @validates_schema(skip_on_field_errors=True)
    def _check_booking_reason(self, data, **kwargs):
        booking = self.context.get('booking')
        has_link = bool(booking.links) if booking else (data.get('link_id') is not None)
        booking_reason = data.get('booking_reason')
        required = rb_settings.get('booking_reason_required')
        validate = False
        if required == BookingReasonRequiredOptions.always:
            validate = True
        elif required == BookingReasonRequiredOptions.not_for_events:
            validate = not has_link
        if validate and not booking_reason:
            raise ValidationError('Booking reason not specified', 'reason')

    @post_load
    def _weekdays_only_weekly(self, data, **kwargs):
        # Make sure we do not set recurrence weekdays if the booking does not have weekly repetition
        if data['repeat_frequency'] != RepeatFrequency.WEEK:
            data['recurrence_weekdays'] = None
        return data


class RoomFeatureSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = RoomFeature
        fields = ('id', 'name', 'title', 'icon')


class EquipmentTypeSchema(mm.SQLAlchemyAutoSchema):
    features = Nested(RoomFeatureSchema, many=True)
    used = Function(lambda eq, ctx: eq.id in ctx['used_ids'])

    class Meta:
        model = EquipmentType
        fields = ('id', 'name', 'features', 'used')


class AdminEquipmentTypeSchema(mm.SQLAlchemyAutoSchema):
    class Meta:
        model = EquipmentType
        fields = ('id', 'name', 'features')


class RoomAttributeSchema(mm.SQLAlchemyAutoSchema):
    hidden = Boolean(attribute='is_hidden')

    class Meta:
        model = RoomAttribute
        fields = ('id', 'name', 'title', 'hidden')


class LocationArgs(mm.Schema):
    class Meta:
        rh_context = ('location',)

    name = fields.String(required=True)
    room_name_format = fields.String(required=True)
    map_url_template = fields.URL(schemes={'http', 'https'}, allow_none=True, load_default='')
    acl_entries = PrincipalPermissionList(LocationPrincipal)

    @validates('name')
    def _check_name_unique(self, name, **kwargs):
        location = self.context['location']
        query = Location.query.filter(~Location.is_deleted, func.lower(Location.name) == name.lower())
        if location:
            query = query.filter(Location.id != location.id)
        if query.has_rows():
            raise ValidationError(_('Name must be unique'))

    @validates('room_name_format')
    def _check_room_name_format_placeholders(self, room_name_format, **kwargs):
        validate_format_placeholders(room_name_format, {'site', 'building', 'floor', 'number'}, {'number'})

    @validates('map_url_template')
    def _check_map_url_template_placeholders(self, map_url_template, **kwargs):
        if not map_url_template:
            return
        validate_format_placeholders(map_url_template, {'id', 'building', 'floor', 'number', 'lat', 'lng'})


class FeatureArgs(mm.Schema):
    class Meta:
        rh_context = ('feature',)

    name = fields.String(validate=validate.Length(min=2), required=True)
    title = fields.String(validate=validate.Length(min=2), required=True)
    icon = fields.String(load_default='')

    @validates('name')
    def _check_name_unique(self, name, **kwargs):
        feature = self.context['feature']
        query = RoomFeature.query.filter(func.lower(RoomFeature.name) == name.lower())
        if feature:
            query = query.filter(RoomFeature.id != feature.id)
        if query.has_rows():
            raise ValidationError(_('Name must be unique'))


class EquipmentTypeArgs(mm.Schema):
    class Meta:
        rh_context = ('equipment_type',)

    name = fields.String(validate=validate.Length(min=2), required=True)
    features = ModelList(RoomFeature, load_default=lambda: [])

    @validates('name')
    def _check_name_unique(self, name, **kwargs):
        equipment_type = self.context['equipment_type']
        query = EquipmentType.query.filter(func.lower(EquipmentType.name) == name.lower())
        if equipment_type:
            query = query.filter(EquipmentType.id != equipment_type.id)
        if query.has_rows():
            raise ValidationError(_('Name must be unique'))


class RoomAttributeArgs(mm.Schema):
    class Meta:
        rh_context = ('attribute',)

    name = fields.String(validate=validate.Length(min=2), required=True)
    title = fields.String(validate=validate.Length(min=2), required=True)
    hidden = fields.Bool(load_default=False)

    @validates('name')
    def _check_name_unique(self, name, **kwargs):
        attribute = self.context['attribute']
        query = RoomAttribute.query.filter(func.lower(RoomAttribute.name) == name.lower())
        if attribute:
            query = query.filter(RoomAttribute.id != attribute.id)
        if query.has_rows():
            raise ValidationError(_('Name must be unique'))


class SettingsSchema(mm.Schema):
    admin_principals = PrincipalList(allow_groups=True)
    authorized_principals = PrincipalList(allow_groups=True)
    managers_edit_rooms = fields.Bool()
    hide_booking_details = fields.Bool()
    hide_module_if_unauthorized = fields.Bool()
    tileserver_url = fields.String(validate=validate.URL(schemes={'http', 'https'}), allow_none=True)
    booking_limit = fields.Int(validate=not_empty)
    booking_reason_required = fields.Enum(BookingReasonRequiredOptions, required=True)
    notifications_enabled = fields.Bool()
    notification_before_days = fields.Int(validate=validate.Range(min=1, max=30))
    notification_before_days_weekly = fields.Int(validate=validate.Range(min=1, max=30))
    notification_before_days_monthly = fields.Int(validate=validate.Range(min=1, max=30))
    end_notifications_enabled = fields.Bool()
    end_notification_daily = fields.Int(validate=validate.Range(min=1, max=30))
    end_notification_weekly = fields.Int(validate=validate.Range(min=1, max=30))
    end_notification_monthly = fields.Int(validate=validate.Range(min=1, max=30))
    internal_notes_enabled = fields.Bool()
    excluded_categories = ModelList(Category)
    grace_period = fields.Int(validate=validate.Range(min=0, max=24), allow_none=True)

    @validates('tileserver_url')
    def _check_tileserver_url_placeholders(self, tileserver_url, **kwargs):
        if tileserver_url is None:
            return
        missing = {x for x in ('{x}', '{y}', '{z}') if x not in tileserver_url}
        if missing:
            # validated client-side, no i18n needed
            raise ValidationError('Missing placeholders: {}'.format(', '.join(missing)))


attributes_schema = AttributesSchema(many=True)
rb_user_schema = RBUserSchema()
rooms_schema = RoomSchema(many=True)
room_attribute_values_schema = RoomAttributeValuesSchema(many=True)
room_update_schema = RoomUpdateSchema()
room_equipment_schema = RoomEquipmentSchema()
map_areas_schema = MapAreaSchema(many=True)
reservation_occurrences_schema = ReservationOccurrenceSchema(many=True)
reservation_occurrences_schema_with_permissions = ReservationOccurrenceSchemaWithPermissions(many=True)
concurrent_pre_bookings_schema = ReservationConcurrentOccurrenceSchema(many=True)
reservation_schema = ReservationSchema()
reservation_details_schema = ReservationDetailsSchema()
reservation_linked_object_data_schema = ReservationLinkedObjectDataSchema()
reservation_user_event_schema = ReservationUserEventSchema(many=True)
blockings_schema = BlockingSchema(many=True)
simple_blockings_schema = BlockingSchema(many=True, only=('id', 'reason'))
nonbookable_periods_schema = NonBookablePeriodSchema(many=True)
nonbookable_periods_admin_schema = NonBookablePeriodAdminSchema(many=True)
bookable_hours_schema = BookableHoursSchema()
locations_schema = LocationsSchema(many=True)
admin_locations_schema = AdminLocationsSchema(many=True)
admin_equipment_type_schema = AdminEquipmentTypeSchema()
room_feature_schema = RoomFeatureSchema()
room_attribute_schema = RoomAttributeSchema()


# legacy api schemas

def _add_server_tz(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_timezone(config.DEFAULT_TIMEZONE))
    return dt


class RoomLegacyAPISchema(RoomSchema):
    # XXX: this schema is legacy due to its camelCased keys; do not use it in any new code
    class Meta(RoomSchema.Meta):
        fields = ('id', 'building', 'name', 'floor', 'longitude', 'latitude', 'number', 'location_name', 'full_name')

    @post_dump
    def _rename_keys(self, data, **kwargs):
        data['fullName'] = data.pop('full_name')
        data['location'] = data.pop('location_name')
        data['roomNr'] = data.pop('number')
        return data


class RoomLegacyMinimalAPISchema(RoomSchema):
    # XXX: this schema is legacy due to its camelCased keys; do not use it in any new code
    class Meta(RoomSchema.Meta):
        fields = ('id', 'full_name')

    @post_dump
    def _rename_keys(self, data, **kwargs):
        data['fullName'] = data.pop('full_name')
        return data


class ReservationLegacyAPISchema(ReservationSchema):
    # XXX: this schema is legacy due to its camelCased keys; do not use it in any new code
    class Meta(ReservationSchema.Meta):
        fields = ('id', 'repeat_frequency', 'repeat_interval', 'booked_for_name',
                  'external_details_url', 'booking_reason', 'is_accepted', 'is_cancelled', 'is_rejected',
                  'location_name', 'contact_email')

    @post_dump(pass_original=True)
    def _hide_sensitive_data(self, data, booking, **kwargs):
        if not booking.can_see_details(self.context.get('user')):
            data['booked_for_name'] = None
            data['contact_email'] = None
        return data

    @post_dump(pass_original=True)
    def _rename_keys(self, data, orig, **kwargs):
        data['startDT'] = _add_server_tz(orig.start_dt)
        data['endDT'] = _add_server_tz(orig.end_dt)
        data['bookedForName'] = data.pop('booked_for_name')
        data['bookingUrl'] = data.pop('external_details_url')
        data['reason'] = data.pop('booking_reason')
        data['isConfirmed'] = data['isValid'] = data.pop('is_accepted')
        data['location'] = data.pop('location_name')
        data['booked_for_user_email'] = data.pop('contact_email')
        return data


class ReservationOccurrenceLegacyAPISchema(ReservationOccurrenceSchema):
    # XXX: this schema is legacy due to its camelCased keys; do not use it in any new code
    class Meta(ReservationOccurrenceSchema.Meta):
        fields = ('is_cancelled', 'is_rejected')

    @post_dump(pass_original=True)
    def _rename_keys(self, data, orig, **kwargs):
        data['startDT'] = _add_server_tz(orig.start_dt)
        data['endDT'] = _add_server_tz(orig.end_dt)
        return data
