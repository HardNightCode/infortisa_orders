# infortisa_orders/models/raw_wizard.py
from odoo import api, fields, models, _


class InfortisaRawWizard(models.TransientModel):
    _name = "infortisa.raw.wizard"
    _description = "Ver XML crudo Infortisa"

    payload = fields.Text("XML enviado", readonly=True)
    response = fields.Text("Respuesta API", readonly=True)

    @api.model
    def open_for_order(self, order_id):
        order = self.env["sale.order"].browse(order_id)
        wiz = self.create({
            "payload": order.infortisa_last_payload or "",
            "response": order.infortisa_last_response or "",
        })
        return {
            "type": "ir.actions.act_window",
            "name": _("Ver XML crudo Infortisa"),
            "res_model": "infortisa.raw.wizard",
            "view_mode": "form",
            "res_id": wiz.id,
            "target": "new",
        }
