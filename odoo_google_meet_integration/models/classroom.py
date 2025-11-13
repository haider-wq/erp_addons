# -*- coding: utf-8 -*-
###############################################################################
#
#    OpenEduCat Inc
#    Copyright (C) 2009-TODAY OpenEduCat Inc(<https://www.openeducat.org>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################

from odoo import models, fields, api
from odoo.exceptions import ValidationError


class OpClassroomInherited(models.Model):
    _inherit = "op.classroom"
    _description = "Classroom"

    meeting_lines = fields.One2many('calendar.event','class_id',string='Asset')


class CalendarEvent(models.Model):
    _inherit = "calendar.event"
    _description = "Classroom Event"

    class_id = fields.Many2one('op.classroom')

    @api.constrains('start', 'stop', 'class_id')
    def _check_classroom_event_same_day_overlap(self):
        for record in self:
            if not record.class_id or not record.start or not record.stop:
                continue

            start_day = record.start.date()

            # Find events in same classroom that start or stop on same date
            overlapping_events = self.search([
                ('id', '!=', record.id),
                ('class_id', '=', record.class_id.id),
                ('start', '<', record.stop),
                ('stop', '>', record.start),
            ])

            for event in overlapping_events:
                if event.start.date() == start_day or event.stop.date() == start_day:
                    raise ValidationError(
                        f"Classroom '{record.class_id.name}' already has a meeting on {start_day.strftime('%Y-%m-%d')} "
                        f"that overlaps in time."
                    )





