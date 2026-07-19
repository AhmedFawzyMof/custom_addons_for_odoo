import json
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class PurchaseOrderChangeLog(models.Model):
    _name = 'purchase.order.change.log'
    _description = 'Purchase Order Change Log'
    _order = 'create_date desc'

    po_id = fields.Many2one('purchase.order', string='Purchase Order', required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one('res.users', string='User', required=True, default=lambda self: self.env.user)
    change_date = fields.Datetime(string='Change Date', default=fields.Datetime.now, readonly=True)
    po_state = fields.Selection([
        ('draft', 'RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('done', 'Locked'),
        ('cancel', 'Cancelled'),
    ], string='PO State at Change', readonly=True)

    field_name = fields.Char(string='Changed Field', readonly=True)
    previous_value = fields.Text(string='Previous Value', readonly=True)
    new_value = fields.Text(string='New Value', readonly=True)
    change_type = fields.Selection([
        ('header', 'Header Field'),
        ('line_add', 'Line Added'),
        ('line_update', 'Line Updated'),
        ('line_remove', 'Line Removed'),
        ('state_change', 'State Change'),
    ], string='Change Type', readonly=True)

    line_id = fields.Many2one('purchase.order.line', string='Order Line', readonly=True, ondelete='set null')
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    affected_picking_ids = fields.Text(string='Affected Pickings (IDs)', readonly=True)
    affected_invoice_ids = fields.Text(string='Affected Invoices (IDs)', readonly=True)

    description = fields.Text(string='Description', readonly=True)

    @api.model
    def log_change(self, po, field_name, previous_value, new_value, change_type='header', line=None,
                   picking_ids=None, invoice_ids=None, description=None):
        log_vals = {
            'po_id': po.id,
            'user_id': self.env.user.id,
            'po_state': po.state,
            'field_name': field_name,
            'previous_value': self._serialize(previous_value),
            'new_value': self._serialize(new_value),
            'change_type': change_type,
            'line_id': line.id if line else None,
            'product_id': line.product_id.id if line and line.product_id else None,
            'affected_picking_ids': json.dumps(list(picking_ids)) if picking_ids else None,
            'affected_invoice_ids': json.dumps(list(invoice_ids)) if invoice_ids else None,
            'description': description or '',
        }
        log = self.create(log_vals)
        _logger.info(
            "PO Change Logged: po=%s (id=%s) field=%s change_type=%s user=%s state=%s",
            po.name, po.id, field_name, change_type, self.env.user.login, po.state,
        )
        return log

    @api.model
    def log_batch_changes(self, po, changes, picking_ids=None, invoice_ids=None):
        """Log multiple changes for a PO.
        changes: list of dicts with keys: field_name, previous_value, new_value, change_type, line, description
        """
        for ch in changes:
            self.log_change(
                po=po,
                field_name=ch.get('field_name'),
                previous_value=ch.get('previous_value'),
                new_value=ch.get('new_value'),
                change_type=ch.get('change_type', 'header'),
                line=ch.get('line'),
                picking_ids=picking_ids or ch.get('picking_ids'),
                invoice_ids=invoice_ids or ch.get('invoice_ids'),
                description=ch.get('description'),
            )

    @api.model
    def _serialize(self, value):
        if value is None:
            return ''
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        if isinstance(value, models.BaseModel):
            return json.dumps({r.id: r.display_name for r in value}, ensure_ascii=False)
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
