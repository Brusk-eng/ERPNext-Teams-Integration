# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.utils import get_time

class TeamsMeeting(Document):
    def after_insert(self):
        self.create_or_link_conference()

    def validate(self):
        self.validate_times()

    def before_save(self):
        self.set_participants_email()

    def on_update(self):
        self.sync_conference_details()

    def create_or_link_conference(self):
        # Image 4108df shows the location dropdown as "Conference Room"[cite: 3]
        if self.location == "Conference Room": 
            # Prevent duplicate creation if already linked
            if self.conference_room_slot:
                return

            booking = frappe.new_doc("Conference Booking")
            
            # Map standard fields based on the Doctype forms[cite: 2, 3]
            booking.date = self.start_date
            booking.from_time = self.start_time
            booking.to_time = self.end_time
            booking.booking_purpose = self.meeting_title or self.description
            
            # Employee is a mandatory field in Conference Booking[cite: 2]
            # Fetch the Employee record linked to the current logged-in user
            employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
            if employee:
                booking.employee = employee
            else:
                frappe.msgprint("No Employee linked to current user. Please set the Employee field manually.")

            # Map the child table (Teams Meeting 'meeting_participants' to Conference Booking 'meeting_members')[cite: 2, 3]
            if self.get("meeting_participants"):
                for participant in self.meeting_participants:
                    booking.append("meeting_members", {
                        "reference_doctype": participant.reference_doctype,
                        "reference_docname": participant.reference_docname,
                        "email": participant.email,
                        "attending": participant.attending
                    })

            # Insert the new booking document
            booking.insert(ignore_permissions=True)
            booking.submit()

            # Link the new Conference Booking back to this Teams Meeting[cite: 3]
            self.db_set("conference_room_slot", booking.name)
            # self.reload()


    def validate_times(self):
        # Basic sanity check so we don't break the space-time continuum 
        if self.start_time and self.end_time:
            if get_time(self.start_time) >= get_time(self.end_time):
                frappe.throw(_("End Time must be after Start Time."))

    def add_participant(self, doctype, docname):
        """Add a single participant to meeting participants

        Args:
                doctype (string): Reference Doctype
                docname (string): Reference Docname
        """
        # FIXED: Changed "event_participants" to "meeting_participants"
        self.append(
            "meeting_participants", 
            {
                "reference_doctype": doctype,
                "reference_docname": docname,
            },
        )
    
    def add_participants(self, participants):
        """Add participant entry

        Args:
                participants ([Array]): Array of a dict with doctype and docname
        """
        for participant in participants:
            self.add_participant(participant["doctype"], participant["docname"])

    def set_participants_email(self):
        # Map Doctypes that store emails directly on their own records
        direct_email_map = {
            "Contact": "email_id",
            "Lead": "email_id",
            "User": "email" 
        }

        for participant in self.get("meeting_participants"):
            if participant.email:
                continue

            ref_type = participant.reference_doctype
            ref_name = participant.reference_docname
            email = None

            # Attempt 1: Try grabbing the email directly if it's a mapped Doctype
            if ref_type in direct_email_map:
                email_field = direct_email_map[ref_type]
                email = frappe.get_value(ref_type, ref_name, email_field)

            # Attempt 2: The ultimate fallback - if it's empty or unmapped, hunt down the default contact
            if not email:
                participant_contact = get_default_contact(ref_type, ref_name)
                
                if participant_contact:
                    email = frappe.get_value("Contact", participant_contact, "email_id")
            
            # Apply whatever we found (even if it's still None, at least we tried!)
            participant.email = email

    def sync_conference_details(self):
        if self.conference_room_slot and frappe.db.exists("Conference Booking", self.conference_room_slot):
            booking = frappe.get_doc("Conference Booking", self.conference_room_slot)

            # Handle cancellations
            if hasattr(self, 'status') and self.status == "Cancelled":
                if booking.docstatus == 1:
                    booking.cancel()
                elif hasattr(booking, 'status'):
                    booking.status = "Cancelled"
                    booking.save(ignore_permissions=True)
                    self.reload()
                return

            # Bypass standard Frappe validation for submitted documents
            if booking.docstatus == 1:
                # Update parent fields directly in the database
                booking.db_set("date", self.start_date)
                booking.db_set("from_time", self.start_time)
                booking.db_set("to_time", self.end_time)
                booking.db_set("booking_purpose", self.meeting_title or self.description)

                # Fetch the child Doctype name dynamically
                child_doctype = booking.meta.get_field("meeting_members").options
                
                # Delete existing child rows directly from the DB
                frappe.db.delete(child_doctype, {"parent": booking.name})
                
                # Insert new child rows directly into the DB
                if self.get("meeting_participants"):
                    for idx, participant in enumerate(self.meeting_participants):
                        child = frappe.new_doc(child_doctype)
                        child.parent = booking.name
                        child.parenttype = booking.doctype
                        child.parentfield = "meeting_members"
                        child.idx = idx + 1
                        child.reference_doctype = participant.reference_doctype
                        child.reference_docname = participant.reference_docname
                        child.attending = participant.attending
                        child.db_insert()
            else:
                # Standard save method if the booking is still a draft (docstatus == 0)
                booking.date = self.start_date
                booking.from_time = self.start_time
                booking.to_time = self.end_time
                booking.booking_purpose = self.meeting_title or self.description

                booking.set("meeting_members", [])
                if self.get("meeting_participants"):
                    for participant in self.meeting_participants:
                        booking.append("meeting_members", {
                            "reference_document_type": participant.reference_doctype,
                            "reference_name": participant.reference_docname,
                            "attending": participant.attending
                        })

                booking.save(ignore_permissions=True)
@frappe.whitelist()
def sync_all_teams_rsvps():
    """
    Scheduled job to auto-sync RSVPs.
    Hook this up in your hooks.py under scheduler_events -> hourly
    """
    # Fetch meetings that are not completed/cancelled AND haven't passed today
    active_meetings = frappe.get_all(
        "Teams Meeting",
        filters={
            "status": ["not in", ["Completed", "Cancelled"]],
            "start_date": [">=", frappe.utils.nowdate()],
            "custom_outlook_event_id": ["is", "set"]
        },
        fields=["name"]
    )

    for meeting in active_meetings:
        try:
            # Assuming get_meeting_rsvps is importable here
            from erpnext_teams_integration.api.meetings import get_meeting_rsvps
            
            res = get_meeting_rsvps(meeting.name, "Teams Meeting")
            
            if not res or not res.get("success"):
                continue
                
            doc = frappe.get_doc("Teams Meeting", meeting.name)
            rsvps = res.get("rsvps", {})
            
            # Flatten into a quick lookup dict for the child table loop
            status_map = {}
            for p in rsvps.get("accepted", []): status_map[p["email"]] = "Yes"
            for p in rsvps.get("declined", []): status_map[p["email"]] = "No"
            for p in rsvps.get("tentative", []): status_map[p["email"]] = "Maybe"
            
            updated = False
            for participant in doc.get("meeting_participants"):
                # If they have an email and they've responded, update the attending column
                if participant.email and participant.email in status_map:
                    new_status = status_map[participant.email]
                    if participant.attending != new_status:
                        participant.attending = new_status
                        updated = True
            
            if updated:
                # Bypass standard validations during background cron execution for speed
                doc.save(ignore_permissions=True)
                frappe.db.commit()

        except Exception as e:
            # Using your existing helper to trap errors quietly
            frappe.log_error(f"Cron RSVP Sync Error for {meeting.name}: {e}", "Teams RSVP Cron")