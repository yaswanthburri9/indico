# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from operator import attrgetter

from wtforms.fields import BooleanField, SelectField, TextAreaField
from wtforms.validators import DataRequired, ValidationError

from indico.modules.events.models.events import EventType
from indico.modules.events.registration.models.forms import RegistrationForm
from indico.modules.events.registration.models.tags import RegistrationTag
from indico.util.date_time import now_utc
from indico.util.i18n import _
from indico.util.string import natural_sort_key
from indico.web.forms.base import IndicoForm, generated_data
from indico.web.forms.fields import (EmailListField, IndicoDateTimeField, IndicoQuerySelectMultipleCheckboxField,
                                     IndicoRadioField, TimeDeltaField)
from indico.web.forms.fields.sqlalchemy import IndicoQuerySelectMultipleTagField
from indico.web.forms.validators import DateTimeRange, HiddenUnless


def _sort_fn(object_list):
    return sorted(object_list, key=lambda x: natural_sort_key(x[1].title))


class ReminderForm(IndicoForm):
    recipient_fields = ['recipients', 'send_to_participants', 'forms', 'tags', 'all_tags', 'send_to_speakers']
    schedule_fields = ['schedule_type', 'absolute_dt', 'relative_delta']
    schedule_recipient_fields = recipient_fields + schedule_fields

    # Schedule
    schedule_type = IndicoRadioField(_('Type'), [DataRequired()],
                                     choices=[('relative', _('Relative to the event start time')),
                                              ('absolute', _('Fixed date/time')),
                                              ('now', _('Send immediately'))])
    relative_delta = TimeDeltaField(_('Offset'), [HiddenUnless('schedule_type', 'relative'), DataRequired()],
                                    units=('weeks', 'days', 'hours'))
    absolute_dt = IndicoDateTimeField(_('Date'), [HiddenUnless('schedule_type', 'absolute'), DataRequired(),
                                                  DateTimeRange()])
    # Recipients
    recipients = EmailListField(_('Email addresses'), description=_('One email address per line.'))
    send_to_participants = BooleanField(_('Participants'),
                                        description=_('Send the reminder to participants/registrants of the event.'))

    forms = IndicoQuerySelectMultipleCheckboxField(_('Filter by forms'),
                                                   [HiddenUnless('send_to_participants')],
                                                   collection_class=set,
                                                   modify_object_list=_sort_fn,
                                                   get_label=attrgetter('title'),
                                                   description=_('Select registration forms here to restrict sending '
                                                                 'the reminder to the selected ones.'))
    tags = IndicoQuerySelectMultipleTagField(_('Filter by tags'), [HiddenUnless('send_to_participants')],
                                             description=_('Limit reminders to participants with these tags.'))
    all_tags = BooleanField(_('All tags must be present'), [HiddenUnless('send_to_participants')],
                            description=_('Participants must have all of the selected tags. '
                                          'Otherwise at least one of them.'))
    send_to_speakers = BooleanField(_('Speakers'),
                                    description=_('Send the reminder to all speakers/chairpersons of the event.'))
    # Misc
    reply_to_address = SelectField(_('Sender'), [DataRequired()],
                                   description=_('The email address that will show up as the sender.'))
    message = TextAreaField(_('Note'), description=_('A custom message to include in the email.'))
    include_summary = BooleanField(_('Include agenda'),
                                   description=_("Includes a simple text version of the event's agenda in the email."))
    include_description = BooleanField(_('Include description'),
                                       description=_("Includes the event's description in the email."))
    attach_ical = BooleanField(_('Attach iCalendar file'),
                               description=_('Attach an iCalendar file to the event reminder.'))

    def __init__(self, *args, **kwargs):
        self.event = kwargs.pop('event')
        self.timezone = self.event.timezone
        super().__init__(*args, **kwargs)
        allowed_senders = self.event.get_allowed_sender_emails(include_noreply=True,
                                                               extra=self.reply_to_address.object_data)
        self.reply_to_address.choices = list(allowed_senders.items())
        if self.event.type_ == EventType.lecture:
            del self.include_summary
        regforms_query = RegistrationForm.query.with_parent(self.event)
        if regforms_query.count() > 1:
            self.forms.query = regforms_query
        else:
            del self.forms
        tags_query = RegistrationTag.query.with_parent(self.event)
        if tags_query.has_rows():
            self.tags.query = tags_query
        else:
            del self.tags
            del self.all_tags

    def validate_recipients(self, field):
        if not field.data and not self.send_to_participants.data and not self.send_to_speakers.data:
            raise ValidationError(_('At least one type of recipient is required.'))

    def validate_send_to_participants(self, field):
        if not field.data and not self.recipients.data and not self.send_to_speakers.data:
            raise ValidationError(_('At least one type of recipient is required.'))

    def validate_send_to_speakers(self, field):
        if not field.data and not self.recipients.data and not self.send_to_participants.data:
            raise ValidationError(_('At least one type of recipient is required.'))

    def validate_schedule_type(self, field):
        # Be graceful and allow a reminder that's in the past but on the same day.
        # It will be sent immediately but that way we are a little bit more user-friendly
        if field.data == 'now':
            return
        scheduled_dt = self.scheduled_dt.data
        if scheduled_dt is not None and scheduled_dt.date() < now_utc().date():
            raise ValidationError(_('The specified date is in the past'))

    @generated_data
    def scheduled_dt(self):
        if self.schedule_type.data == 'absolute':
            if self.absolute_dt.data is None:
                return None
            return self.absolute_dt.data
        elif self.schedule_type.data == 'relative':
            if self.relative_delta.data is None:
                return None
            return self.event.start_dt - self.relative_delta.data
        elif self.schedule_type.data == 'now':
            return now_utc()

    @generated_data
    def event_start_delta(self):
        return self.relative_delta.data if self.schedule_type.data == 'relative' else None
