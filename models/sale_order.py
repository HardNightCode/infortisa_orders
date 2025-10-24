# -*- coding: utf-8 -*-
import logging
import requests
import json
import xml.etree.ElementTree as ET
import re
import base64

from xml.sax.saxutils import escape as xml_escape
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

INFORTISA_BASE = "https://apiv2.infortisa.com"
# Estados en los que NO debemos intentar crear factura/pago/XML
BLOCKED_CODE_PREFIXES = ("VX/", "VN/", "VA/", "HR/")  # sin stock, anulado, empaquetado, pagado


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    infortisa_api_key = fields.Char(
        string="Infortisa API Key", config_parameter="infortisa.api_key"
    )
    infortisa_test_mode = fields.Boolean(
        string="Infortisa Modo TEST",
        config_parameter="infortisa.test_mode",
        default=False,  # <- cambiado (antes True)
    )
    infortisa_default_block = fields.Boolean(
        string="Crear pedido bloqueado por defecto",
        config_parameter="infortisa.default_block",
        default=False,  # <- cambiado (antes True)
    )
    # Facturación proveedor
    infortisa_vendor_id = fields.Many2one(
        "res.partner",
        string="Proveedor Infortisa",
        domain="[('is_company','=',True)]",
        config_parameter="infortisa.vendor_id",
    )
    infortisa_product_purchase_id = fields.Many2one(
        "product.product",
        string="Producto (base coste)",
        config_parameter="infortisa.product_purchase_id",
    )
    infortisa_product_shipping_id = fields.Many2one(
        "product.product",
        string="Producto (portes)",
        config_parameter="infortisa.product_shipping_id",
    )
    infortisa_journal_id = fields.Many2one(
        "account.journal",
        string="Diario proveedor (compras)",
        domain="[('type','=','purchase')]",
        config_parameter="infortisa.journal_id",
    )

    # NUEVO: pagos automáticos
    infortisa_bank_journal_payment_id = fields.Many2one(
        "account.journal",
        string="Diario de banco para pagos",
        domain="[('type','=','bank')]",
        config_parameter="infortisa.bank_journal_payment_id",
    )
    infortisa_auto_create_bill = fields.Boolean(
        string="Auto-crear factura con referencia de transferencia",
        config_parameter="infortisa.auto_create_bill",
        default=False,
    )


class SaleOrder(models.Model):
    _inherit = "sale.order"

    # ---- Trazas de Infortisa
    infortisa_customer_ref = fields.Char("Ref. Cliente (Infortisa)", copy=False)
    infortisa_internal_ref = fields.Char("Ref. Interna (Infortisa)", copy=False, readonly=True)
    infortisa_op_code = fields.Char("Codigo operacion (Infortisa)", copy=False, readonly=True)
    infortisa_state = fields.Char("Estado Infortisa", copy=False, readonly=True)
    infortisa_last_payload = fields.Text("XML enviado (crudo)", copy=False, readonly=True)
    infortisa_last_response = fields.Text("Ultima respuesta (crudo)", copy=False, readonly=True)
    infortisa_sent = fields.Boolean("Enviado a Infortisa", default=False, copy=False)

    # ---- Importes del API (se actualizan al consultar estado)
    currency_id = fields.Many2one(related="pricelist_id.currency_id", store=True, readonly=True)
    infortisa_amount_base = fields.Monetary("Base (API)", readonly=True)  # suma de PriceWithoutCanon*Qty
    infortisa_amount_canon_op = fields.Monetary("Canon LPI (operacion)", readonly=True)
    infortisa_amount_other_op = fields.Monetary("Otros costes (operacion)", readonly=True)
    infortisa_amount_shipping = fields.Monetary("Portes (API)", readonly=True)
    infortisa_amount_tax = fields.Monetary("Impuestos (API)", readonly=True)
    infortisa_amount_total = fields.Monetary("Total (API)", readonly=True)

    # Detalle productos del API renderizado (HTML de solo lectura)
    infortisa_products_html = fields.Html("Detalle productos (API)", readonly=True, sanitize=False)

    # --- Resumen humano (solo lectura)
    infortisa_mode_display = fields.Char(
        "Modo Infortisa", readonly=True, compute="_compute_infortisa_summary", store=False
    )
    infortisa_delivery_type = fields.Char(
        "Tipo de envio", readonly=True, compute="_compute_infortisa_summary", store=False
    )
    infortisa_shipping_display = fields.Text(
        "Direccion de entrega", readonly=True, compute="_compute_infortisa_summary", store=False
    )
    infortisa_comment_display = fields.Char(
        "Comentarios", readonly=True, compute="_compute_infortisa_summary", store=False
    )

    # ---- Enlace a la factura de proveedor creada desde Infortisa
    infortisa_vendor_bill_id = fields.Many2one(
        "account.move",
        string="Factura proveedor Infortisa",
        copy=False,
        readonly=True,
        domain=[("move_type", "=", "in_invoice")],
    )

    # ---- Referencia transferencia + pago proveedor
    infortisa_transfer_ref = fields.Char("Referencia transferencia (Infortisa)", copy=False, readonly=True)
    infortisa_vendor_payment_id = fields.Many2one(
        "account.payment",
        string="Pago proveedor Infortisa",
        copy=False,
        readonly=True,
        domain=[("partner_type", "=", "supplier")],
    )

    infortisa_payment_state = fields.Selection(
        [
            ("missing", "Sin pago"),
            ("to_export", "Pago pendiente de exportar"),
            ("exported", "XML ISO20022 generado"),
            ("posted", "Pago contabilizado"),
            ("failed", "Error al generar pago/XML"),
        ],
        string="Estado pago Infortisa",
        default="missing",
        readonly=True,
        copy=False,
    )

    # --- Tracking (nuevo)
    infortisa_tracking_url = fields.Char("Tracking URL (Infortisa)", copy=False, readonly=True)
    infortisa_tracking_number = fields.Char("Tracking Number (Infortisa)", copy=False, readonly=True)
    infortisa_tracking_status = fields.Char("Tracking Status (Infortisa)", copy=False, readonly=True)
    infortisa_tracking_status_detail = fields.Char("Tracking Detail (Infortisa)", copy=False, readonly=True)

    # --- Gatekeeper: sólo usar Infortisa si hay al menos 1 línea con el proveedor configurado
    infortisa_allowed = fields.Boolean(
        string="Usar flujo Infortisa",
        compute="_compute_infortisa_allowed",
        compute_sudo=True,            # <- importante para que el compute no falle por permisos en upgrade
        store=True,
        readonly=True,
    )

    def _infortisa_vendor_partner(self):
        """Devuelve el partner configurado en Ajustes > Infortisa como proveedor."""
        ICP = self.env["ir.config_parameter"].sudo()
        vid = ICP.get_param("infortisa.vendor_id")
        try:
            vid_int = int(vid or 0)
        except Exception:
            vid_int = 0
        return self.env["res.partner"].browse(vid_int) if vid_int else self.env["res.partner"].browse(False)

    def _line_has_infortisa_vendor(self, line, vendor_partner):
        """True si la línea pertenece a un producto que tiene como proveedor al partner indicado."""
        if not vendor_partner or not vendor_partner.exists():
            return False
        if not line or line.display_type or getattr(line, "is_delivery", False):
            return False
        product = line.product_id
        if not product:
            return False

        # Odoo estándar: proveedores del producto = seller_ids (model: product.supplierinfo)
        # ¡OJO! El campo correcto es partner_id, NO name.
        sellers = product.seller_ids.filtered(
            lambda s: s.partner_id and s.partner_id.id == vendor_partner.id
        )

        # (Opcional) Si usáis un campo personalizado en el producto para “Proveedor”, descomenta/adapta:
        # if hasattr(product, 'x_studio_proveedor') and product.x_studio_proveedor and product.x_studio_proveedor.id == vendor_partner.id:
        #     return True

        return bool(sellers)

    @api.depends(
        "order_line.product_id",
        "order_line.product_id.seller_ids.partner_id",  # <- dependemos de partner_id, no de 'name'
        "order_line.display_type",
        "order_line.is_delivery",
    )
    def _compute_infortisa_allowed(self):
        vendor = self._infortisa_vendor_partner()
        for order in self:
            allowed = False
            if vendor and vendor.exists():
                for l in order.order_line:
                    # Protección extra: nunca reventar el compute durante una carga/upgrade
                    try:
                        if order._line_has_infortisa_vendor(l, vendor):
                            allowed = True
                            break
                    except Exception:
                        continue
            order.infortisa_allowed = allowed

    # ===== util =====
    def _is_ceuta_address(self, partner):
        """Detecta si un partner representa una dirección de CEUTA (ES)."""
        if not partner:
            return False
        zipc = (partner.zip or "").strip()
        city = (partner.city or "").strip().upper()
        st_name = ((partner.state_id and partner.state_id.name) or "").strip().upper()
        st_code = ((partner.state_id and partner.state_id.code) or "").strip().upper()
        cc = ((partner.country_id and partner.country_id.code) or "ES").upper()

        if cc != "ES":
            return False

        # Señales fuertes
        if zipc.startswith("510"):
            return True
        if "CEUTA" in city:
            return True

        # El estado "CE/CEUTA" solo cuenta si además cuadra zip o ciudad
        if (st_name == "CEUTA" or st_code == "CE") and (zipc.startswith("510") or "CEUTA" in city):
            return True

        return False

    def _infortisa_get_effective_shipping_partner(self):
        """Devuelve el partner de 'envío efectivo' (lo elegido en el checkout).
        Prioriza partner_shipping_id si existe; si no, partner_id."""
        self.ensure_one()
        return self.partner_shipping_id or self.partner_id

    def _infortisa_build_shipping_values(self):
        """Devuelve (ship_dict, use_ceuta_override) partiendo SIEMPRE de la dirección
        efectiva de envío (checkout). Sirve para XML y para el Resumen."""
        self.ensure_one()
        p = self._infortisa_get_effective_shipping_partner()

        use_ceuta = self._is_ceuta_address(p)

        if use_ceuta:
            ship = {
                "company": "SÁNCHEZ PEINADO S.L",
                "contact": "Federico Conejero",
                "phone":   "600589947",
                "addr1":   "CL ARRABAL PARCELA 10, Pol. Guadarranque",
                "addr2":   "NAVE TDN-LOGITRANS",
                "zip":     "11360",
                "city":    "SAN ROQUE",
                "cc":      "ES",
            }
        else:
            ship = {
                "company": (p.name or "Cliente")[:50],
                "contact": p.name or "",
                "phone":   p.phone or p.mobile or "",
                "addr1":   (p.street or "")[:40],
                "addr2":   (p.street2 or "")[:40],
                "zip":     p.zip or "",
                "city":    p.city or "",
                "cc":      (p.country_id and p.country_id.code) or "ES",
            }
        return ship, use_ceuta

    def _icp_bool(self, key, default=False):
        """Lectura robusta de booleans en ir.config_parameter."""
        val = self.env["ir.config_parameter"].sudo().get_param(key, str(bool(default)))
        return str(val).strip().lower() in ("true", "1", "yes", "y", "t")

    @api.depends("partner_shipping_id", "partner_id", "note")
    def _compute_infortisa_summary(self):
        for order in self:
            # 1) Modo
            test_mode = order._icp_bool("infortisa.test_mode", False)
            order.infortisa_mode_display = "TEST" if test_mode else "REAL"
            # 2) Tipo de envío
            order.infortisa_delivery_type = "ENV"

            # 3) Dirección compuesta (siempre desde la dirección efectiva/checkout)
            ship, use_ceuta = order._infortisa_build_shipping_values()
            lines = [
                ship.get("company") or "",
                ship.get("contact") or "",
                ship.get("phone") or "",
                f"{ship.get('addr1','')} {ship.get('addr2','')}".strip(),
                f"{ship.get('zip','')} {ship.get('city','')} ({ship.get('cc','')})".strip(),
            ]
            order.infortisa_shipping_display = "\n".join([l for l in lines if l]).strip()

            # 4) Comentarios limpios
            try:
                order.infortisa_comment_display = order._clean_text_for_xml(order.note)
            except Exception:
                txt = (order.note or "").strip()
                for junk in ["<p>", "</p>", "<br/>", "<br>", "&nbsp;"]:
                    txt = txt.replace(junk, " ")
                order.infortisa_comment_display = " ".join(txt.split())[:100]

    def _get_infortisa_headers(self):
        api_key = self.env["ir.config_parameter"].sudo().get_param("infortisa.api_key")
        if not api_key:
            raise UserError(_("Falta la API Key de Infortisa (Ajustes > Infortisa)."))
        return {
            "Authorization-Token": api_key,
            "Accept": "text/xml",
            "Content-Type": "text/xml; charset=utf-16",
        }

    def action_infortisa_open_raw(self):
        self.ensure_one()
        return self.env["infortisa.raw.wizard"].open_for_order(self.id)

    @staticmethod
    def _clean_text_for_xml(text, max_len=100):
        txt = (text or "")
        txt = re.sub(r"<[^>]*>", " ", txt, flags=re.S)
        from html import unescape
        txt = unescape(txt)
        txt = " ".join(txt.split())[:max_len]
        return xml_escape(txt)

    # ---------- Métodos soporte ISO20022/SEPA (dentro de la clase) ----------
    def _get_iso20022_method_line(self, bank_journal):
        """Devuelve la payment.method.line del diario de banco para ISO20022 (SEPA Credit Transfer)."""
        self.ensure_one()
        pm_line = self.env["account.payment.method.line"].search([
            ("journal_id", "=", bank_journal.id),
            ("payment_type", "=", "outbound"),
            "|", ("name", "ilike", "ISO20022"), ("name", "ilike", "SEPA"),
        ], limit=1)
        if not pm_line:
            pm = self.env["account.payment.method"].search([
                ("payment_type", "=", "outbound"),
                ("code", "in", ["iso20022", "sepa_ct", "sepa_credit_transfer"]),
            ], limit=1)
            if pm:
                pm_line = self.env["account.payment.method.line"].create({
                    "name": pm.name or "ISO20022 Credit Transfer",
                    "payment_method_id": pm.id,
                    "payment_type": "outbound",
                    "journal_id": bank_journal.id,
                })
        return pm_line

    def _get_vendor_bank_account(self, partner):
        bank = self.env["res.partner.bank"].search([("partner_id", "=", partner.id)], limit=1)
        return bank

    def _find_bank_journal(self):
        """Prioriza el configurado; si no, intenta código corto BNK5, luego 'Banco', luego el primero tipo bank."""
        ICP = self.env["ir.config_parameter"].sudo()
        j_id = ICP.get_param("infortisa.bank_journal_payment_id")
        if j_id:
            j = self.env["account.journal"].browse(int(j_id))
            if j and j.exists():
                return j
        j = self.env["account.journal"].search([("type", "=", "bank"), ("code", "=", "BNK5")], limit=1)
        if j:
            return j
        j = self.env["account.journal"].search([("type", "=", "bank"), ("name", "ilike", "Banco")], limit=1)
        if j:
            return j
        return self.env["account.journal"].search([("type", "=", "bank")], limit=1)

    def _create_vendor_payment_and_xml(self):
        """Crea/reutiliza el pago proveedor, lo contabiliza, lo mete en un lote (1 pago) y genera XML ISO20022 si es posible."""
        self.ensure_one()
        order = self

        # No permitir si el Code está en un estado no pagadero (VX/VN/VA/HR)
        code = (order.infortisa_op_code or "")
        if any(code.startswith(p) for p in BLOCKED_CODE_PREFIXES):
            order.infortisa_payment_state = "missing"
            order.message_post(body=_("Pago/XML bloqueado: Code=%s (estado no pagadero).") % code)
            return False

        bill = order.infortisa_vendor_bill_id
        if not bill or bill.state != "posted":
            if bill and bill.state == "draft":
                bill.action_post()
            else:
                return False

        if not order.infortisa_transfer_ref:
            return False

        # --- Diario banco
        bank_journal = order._find_bank_journal()
        if not bank_journal:
            order.infortisa_payment_state = "failed"
            order.message_post(body=_("No se encontró un diario de banco para crear el pago ISO20022."))
            return False

        # --- Método ISO20022 / SEPA
        pm_line = order._get_iso20022_method_line(bank_journal)
        if not pm_line:
            order.infortisa_payment_state = "failed"
            order.message_post(body=_("No se encontró/creó el método 'ISO20022 Credit Transfer' en el diario de banco."))
            return False

        # --- Cuenta bancaria del proveedor
        vendor = bill.partner_id
        partner_bank = order._get_vendor_bank_account(vendor)
        if not partner_bank:
            order.infortisa_payment_state = "failed"
            order.message_post(body=_("El proveedor no tiene cuenta bancaria configurada (Contabilidad > Proveedores > Proveedor)."))
            return False

        # --- Crear o reutilizar pago (¡sin return temprano!)
        payment = order.infortisa_vendor_payment_id
        if not payment:
            pay_vals = {
                "payment_type": "outbound",
                "partner_type": "supplier",
                "partner_id": vendor.id,
                "amount": abs(bill.amount_residual),
                "currency_id": bill.currency_id.id,
                "date": fields.Date.context_today(self),
                "journal_id": bank_journal.id,
                "payment_method_line_id": pm_line.id,
                # Referencia visible del pago = VR/...
                "payment_reference": order.infortisa_transfer_ref or _("Pago Infortisa %s") % order.name,
                "memo": order.infortisa_transfer_ref or _("Pago Infortisa %s") % order.name,
                "partner_bank_id": partner_bank.id,
            }
            payment = self.env["account.payment"].create(pay_vals)
            order.infortisa_vendor_payment_id = payment.id

        # Asegurar memo / referencia del pago = VR/...
        vr = order.infortisa_transfer_ref or _("Pago Infortisa %s") % order.name
        upd = {}
        if vr:
            if (payment.memo or "") != vr:
                upd["memo"] = vr
            if (payment.payment_reference or "") != vr:
                upd["payment_reference"] = vr
        if upd:
            payment.write(upd)

        # Contabilizar si hace falta
        if payment.state != "posted":
            try:
                payment.action_post()
                order.infortisa_payment_state = "posted"
            except Exception as e:
                order.infortisa_payment_state = "failed"
                order.message_post(body=_("Error al contabilizar el pago: %s") % e)
                return False

        # Conciliar con la factura (si procede)
        try:
            payable_lines = bill.line_ids.filtered(lambda l: l.account_id.internal_type == "payable")
            if payable_lines:
                (payment.line_ids + payable_lines).reconcile()
        except Exception:
            pass

        # --- Asegurar BATCH (pago por lotes) ---
        try:
            # 0) Comprobación dura: ¿están los módulos?
            mod = self.env["ir.module.module"].sudo().search([("name", "=", "account_batch_payment")], limit=1)
            if not (mod and mod.state == "installed"):
                raise UserError(_("El módulo 'account_batch_payment' no está instalado."))

            mod2 = self.env["ir.module.module"].sudo().search([("name", "=", "account_iso20022")], limit=1)
            if not (mod2 and mod2.state == "installed"):
                raise UserError(_("El módulo 'account_iso20022' no está instalado."))

            Batch = self.env["account.batch.payment"].sudo()

            # 1) En Odoo 18 el lote usa el payment.method (no la line)
            pm = pm_line.payment_method_id
            if not pm:
                raise UserError(_("No se encontró el payment.method asociado a la línea de método."))

            # 2) Forzar en el pago banderas que algunas bases requieren para exportar
            upd = {}
            if hasattr(payment, "use_electronic_payment_method") and not payment.use_electronic_payment_method:
                upd["use_electronic_payment_method"] = True
            if hasattr(payment, "payment_method_id") and payment.payment_method_id.id != pm.id:
                upd["payment_method_id"] = pm.id
            if hasattr(payment, "payment_method_code") and (payment.payment_method_code or "") != (pm.code or ""):
                upd["payment_method_code"] = pm.code or False
            # versión PAIN (si existe el campo)
            if hasattr(payment, "sepa_pain_version") and not payment.sepa_pain_version:
                upd["sepa_pain_version"] = "pain.001.001.03"
            if upd:
                payment.write(upd)

            # 3) Reutilizar lote compatible o crear uno nuevo
            batch = payment.batch_payment_id
            compatible = bool(batch and
                              batch.journal_id.id == payment.journal_id.id and
                              batch.batch_type == payment.payment_type and
                              (batch.payment_method_id.id == pm.id or batch.payment_method_code == pm.code))
            if not compatible:
                batch = False

            if not batch:
                lote_name = _("Infortisa %s") % (order.infortisa_transfer_ref or order.name,)

                batch_vals = {
                    "name": lote_name,
                    "journal_id": payment.journal_id.id,
                    "batch_type": payment.payment_type,   # 'outbound'
                    "payment_method_id": pm.id,
                    "payment_method_code": pm.code or False,
                    "payment_ids": [(6, 0, [payment.id])],
                    "company_id": payment.company_id.id,
                    "currency_id": payment.currency_id.id,
                    "date": fields.Date.context_today(self),
                    "file_generation_enabled": True,
                }
                # limpia falsy
                batch_vals = {k: v for k, v in batch_vals.items() if v}
                batch = Batch.create(batch_vals)
                order.message_post(body=_("Lote de pagos creado: %s (método: %s)") % (batch.display_name, pm.code))
            else:
                if payment.id not in batch.payment_ids.ids:
                    batch.write({"payment_ids": [(4, payment.id)]})
                    order.message_post(body=_("Pago añadido al lote existente: %s") % (batch.display_name,))

            # --- Generar XML desde el lote (robusto) ---
            try:
                def _normalize_export(ret, batch, payment):
                    """Devuelve (xml_bytes, file_name) a partir de múltiples variantes de retorno/campo/adjunto."""
                    filename = f"iso20022_{batch.name or batch.id}.xml"
                    candidates = []

                    # A) La acción devuelve algo utilizable
                    if isinstance(ret, dict):
                        # Puede venir {'file': bytes/str} o {'file': {'file': bytes/str, 'filename': 'x.xml'}}
                        f = ret.get("file")
                        if isinstance(f, dict):
                            if f.get("filename"):
                                filename = f["filename"]
                            candidates.append(f.get("file"))
                        else:
                            candidates.append(f)
                        # Algunas ediciones devuelven 'filename' directo
                        if ret.get("filename"):
                            filename = ret["filename"]

                    # B) Campos en el lote según edición
                    # - export_file: binario base64 (común)
                    xml_field = getattr(batch, "export_file", False)
                    if xml_field:
                        candidates.append(xml_field)
                    # - export_file_id: M2O a ir.attachment (18.0+ en algunas builds)
                    export_file_id = getattr(batch, "export_file_id", False)
                    if export_file_id and getattr(export_file_id, "datas", False):
                        candidates.append(export_file_id.datas)
                        filename = export_file_id.name or filename
                    # - sepa_xml_file: binario legacy
                    sepa_xml = getattr(batch, "sepa_xml_file", False)
                    if sepa_xml:
                        candidates.append(sepa_xml)

                    # C) Fallback: adjuntos ya creados por la acción
                    Att = self.env["ir.attachment"].sudo()

                    # 1) Buscar primero en el LOTE
                    xml_att = Att.search([
                        ("res_model", "=", "account.batch.payment"),
                        ("res_id", "=", batch.id),
                        "|", ("mimetype", "ilike", "xml"), ("name", "ilike", ".xml"),
                    ], order="id desc", limit=1)
                    if xml_att and xml_att.datas:
                        import base64 as _b64
                        try:
                            decoded = _b64.b64decode(xml_att.datas)
                        except Exception:
                            decoded = b""
                        if decoded:
                            return decoded, (xml_att.name or filename)

                    # 2) Algunas ediciones adjuntan en el PAGO
                    if payment:
                        xml_att2 = Att.search([
                            ("res_model", "=", "account.payment"),
                            ("res_id", "=", payment.id),
                            "|", ("mimetype", "ilike", "xml"), ("name", "ilike", ".xml"),
                        ], order="id desc", limit=1)
                        if xml_att2 and xml_att2.datas:
                            import base64 as _b64
                            try:
                                decoded = _b64.b64decode(xml_att2.datas)
                            except Exception:
                                decoded = b""
                            if decoded:
                                return decoded, (xml_att2.name or filename)

                    # D) Último recurso: método privado que devuelve bytes/base64
                    if hasattr(batch, "_generate_export_file"):
                        try:
                            candidates.append(batch._generate_export_file())
                        except Exception:
                            pass

                    # Normalización de candidatos (bytes/base64/str)
                    import base64 as _b64
                    for c in candidates:
                        if not c:
                            continue
                        if isinstance(c, (bytes, bytearray, memoryview)):
                            raw = bytes(c)
                            # Si parece base64, intenta decodificar y ver si empieza por '<?xml'
                            try:
                                decoded = _b64.b64decode(raw, validate=True)
                                if decoded.strip().startswith(b'<?xml'):
                                    return decoded, filename
                            except Exception:
                                pass
                            # Si no es base64 válido, puede ser XML plano
                            if raw.strip().startswith(b'<?xml'):
                                return raw, filename
                            return raw, filename
                        if isinstance(c, str):
                            s = c.strip()
                            try:
                                decoded = _b64.b64decode(s, validate=True)
                                if decoded.strip().startswith(b'<?xml'):
                                    return decoded, filename
                            except Exception:
                                pass
                            # Texto plano: úsalo (por si ya viene XML)
                            return s.encode("utf-8"), filename

                    return None, filename

                # Ejecuta la acción que corresponda en tu build
                ret = None
                if hasattr(batch, "action_validate_generate_file"):
                    ret = batch.action_validate_generate_file()
                elif hasattr(batch, "action_validate_generate_xml"):
                    ret = batch.action_validate_generate_xml()
                # (otras ediciones no devuelven nada y sólo crean un adjunto/campo)

                xml_bytes, export_name = _normalize_export(ret, batch, payment)

                if xml_bytes:
                    # Evita duplicar por nombre exacto en el LOTE
                    Att = self.env["ir.attachment"].sudo()
                    existing = Att.search([
                        ("res_model", "=", "account.batch.payment"),
                        ("res_id",   "=", batch.id),
                        ("name",     "=", export_name),
                    ], limit=1)
                    if not existing:
                        att = Att.create({
                            "name": export_name,
                            "res_model": "account.batch.payment",
                            "res_id": batch.id,
                            "type": "binary",
                            "datas": base64.b64encode(xml_bytes),
                            "mimetype": "application/xml",
                        })
                    else:
                        att = existing

                    batch.message_post(body=_("XML ISO20022 generado."), attachment_ids=[att.id])
                    order.message_post(body=_("XML ISO20022 generado en el lote de pagos."), attachment_ids=[att.id])
                    order.infortisa_payment_state = "exported"
                else:
                    order.infortisa_payment_state = "to_export"
                    order.message_post(body=_("No se obtuvo contenido para el XML ISO20022 del lote (revisar método/export_file/export_file_id/adjuntos)."))

            except Exception as e:
                order.infortisa_payment_state = "failed"
                order.message_post(body=_("Error al generar el XML del lote: %s") % e)

        except Exception as e:
            order.infortisa_payment_state = "failed"
            order.message_post(body=_("Error al crear el lote o preparar la exportación: %s") % e)
            return False

        return True

    def _auto_make_payment_if_ready(self):
        # """Llamado por cron: si hay factura y referencia, crea pago + XML ISO20022,
        #   únicamente cuando Code sea VR/. Con VX/ o VN/ no hace nada."""
        for order in self:
            # Gatekeeper
            if not order.infortisa_allowed:
                continue
            try:
                if not order.infortisa_sent:
                    continue

                code = (order.infortisa_op_code or "").strip()

                # 1) Bloqueo duro: VX/VN/VA/HR -> NO crear nada
                if any(code.startswith(p) for p in BLOCKED_CODE_PREFIXES):
                    if order.infortisa_payment_state != "missing":
                        order.infortisa_payment_state = "missing"
                    # (opcional) Deja el post si quieres traza, o comenta para cero ruido
                    order.message_post(body=_("Cron: Code=%s indica estado no pagadero; no se crea factura/pago/XML.") % (code or "(vacío)"))
                    continue

                # 2) Solo avanzar automáticamente cuando sea VR/
                if not code.startswith("VR/"):
                    continue

                # 3) Requiere referencia de transferencia para seguir
                if not order.infortisa_transfer_ref:
                    continue

                # 4) Crear factura automáticamente si está activado y no existe
                auto_bill = order._icp_bool("infortisa.auto_create_bill", False)
                if auto_bill and not order.infortisa_vendor_bill_id:
                    try:
                        order.action_infortisa_create_bill()
                        # Propaga la referencia a la factura recién creada
                        bill2 = order.infortisa_vendor_bill_id
                        if bill2 and order.infortisa_transfer_ref:
                            vals = {}
                            if getattr(bill2, "payment_reference", None) != order.infortisa_transfer_ref:
                                vals["payment_reference"] = order.infortisa_transfer_ref
                            if (bill2.ref or "") != order.infortisa_transfer_ref:
                                vals["ref"] = order.infortisa_transfer_ref
                            if vals:
                                bill2.write(vals)
                                bill2.message_post(body=_("Factura creada automaticamente y referenciada: %s") % order.infortisa_transfer_ref)
                    except Exception as e:
                        order.message_post(body=_("No se pudo crear la factura automaticamente: %s") % e)

                # 5) Si ya hay factura, y aún no hay pago, crear pago + lote + XML
                if order.infortisa_vendor_bill_id and not order.infortisa_vendor_payment_id:
                    order._create_vendor_payment_and_xml()

            except Exception as e:
                order.infortisa_payment_state = "failed"
                order.message_post(body=_("Error en auto-generacion de pago ISO20022: %s") % e)

    # ========== 1) CREAR PEDIDO EN INFORTISA ==========
    def action_infortisa_send(self, block=None, test=None):
        for order in self:
            # Gatekeeper: si no está permitido por proveedor, saltar
            if not order.infortisa_allowed:
                continue

            if order.infortisa_sent:
                raise UserError(_("Este pedido ya fue enviado a Infortisa."))

            if test is None:
                test = order._icp_bool("infortisa.test_mode", False)
            if block is None:
                block = order._icp_bool("infortisa.default_block", False)

            if not order.infortisa_customer_ref:
                order.infortisa_customer_ref = (order.name or "").replace("/", "").replace(" ", "")

            _x = lambda s: xml_escape((s or "").strip())

            delivery_comment = self._clean_text_for_xml(order.note) or "Pedido web"
            delivery_type = "ENV"

            # SIEMPRE: dirección efectiva del checkout
            ship, use_ceuta_override = order._infortisa_build_shipping_values()
            if use_ceuta_override:
                order.message_post(body=_("Dirección CEUTA detectada en el envío efectivo (checkout): se fuerza envío a almacén de San Roque en el XML de Infortisa."))

            xml_parts = [
                '<?xml version="1.0" encoding="utf-16"?>',
                '<Order xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
                f"  <Test>{str(test).lower()}</Test>",
                f"  <CustomerReference>{_x(order.infortisa_customer_ref)}</CustomerReference>",
                "  <ShopNumber></ShopNumber>",
                f"  <DeliveryType>{_x(delivery_type)}</DeliveryType>",
                f"  <BlockOrder>{str(block).lower()}</BlockOrder>",
                f"  <DeliveryComment>{_x(delivery_comment)}</DeliveryComment>",
                "  <ShippingAddress>",
                f"    <Company>{_x((ship.get('company') or 'Cliente')[:50])}</Company>",
                f"    <Contact>{_x(ship.get('contact'))}</Contact>",
                f"    <PhoneNumber>{_x(ship.get('phone') or '')}</PhoneNumber>",
                f"    <Address1>{_x((ship.get('addr1') or '')[:40])}</Address1>",
                f"    <Address2>{_x((ship.get('addr2') or '')[:40])}</Address2>",
                f"    <ZipCode>{_x(ship.get('zip') or '')}</ZipCode>",
                f"    <City>{_x(ship.get('city') or '')}</City>",
                f"    <CountryTwoLetterCode>{_x(ship.get('cc') or 'ES')}</CountryTwoLetterCode>",
                "  </ShippingAddress>",
                "  <Products>",
            ]

            has_any_product = False
            for line in order.order_line:
                if line.display_type:
                    continue
                if getattr(line, "is_delivery", False):
                    continue
                if not line.product_id:
                    continue
                sku = (line.product_id.default_code or "").strip()
                if not sku:
                    continue
                qty = int(round(line.product_uom_qty))
                if qty <= 0:
                    continue
                has_any_product = True
                xml_parts += [
                    "    <Product>",
                    f"      <SKU>{_x(sku)}</SKU>",
                    "      <Partnumber></Partnumber>",
                    f"      <Quantity>{qty}</Quantity>",
                    "    </Product>",
                ]

            if not has_any_product:
                raise UserError(_("No hay líneas válidas para enviar a Infortisa (SKU y cantidad)."))

            xml_parts += [
                "  </Products>",
                "</Order>",
            ]
            xml_body = "\n".join(xml_parts)
            payload_bytes = xml_body.encode("utf-16")

            headers = order._get_infortisa_headers()
            url = f"{INFORTISA_BASE}/api/order/create"

            resp = requests.post(url, headers=headers, data=payload_bytes, timeout=60)
            order.write({
                "infortisa_last_payload": xml_body,
                "infortisa_last_response": resp.text,
            })
            if resp.status_code not in (200, 201):
                raise UserError(_("Error Infortisa (HTTP %s): %s") % (resp.status_code, resp.text))

            # -------- PARSEO ROBUSTO DE INTERNAL REFERENCE (EXT...) --------
            internal_ref = None
            try:
                # Algunas respuestas vienen con namespace; otras no. Probamos ambos.
                root = ET.fromstring(resp.text)
                NS = {'n': 'http://schemas.datacontract.org/2004/07/BackEnd.Data.Npoco.Models'}
                # 1) Campo estándar
                el = root.find(".//n:InternalReference", NS) or root.find(".//InternalReference")
                if el is not None and (el.text or "") and not el.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil"):
                    internal_ref = (el.text or "").strip()

                # 2) Fallback: puede venir sólo en el DeliveryComment como "... Pedido API EXT123456 ..."
                if not internal_ref:
                    dc = root.find(".//n:DeliveryComment", NS) or root.find(".//DeliveryComment")
                    if dc is not None and dc.text:
                        m = re.search(r"\bEXT\d+\b", dc.text)
                        if m:
                            internal_ref = m.group(0)

            except Exception:
                # Último recurso: substring naïf si el XML trae el tag literal
                if "<InternalReference>" in resp.text:
                    internal_ref = resp.text.split("<InternalReference>")[1].split("</InternalReference>")[0].strip()

            # Si la API reporta errores, paramos aquí
            if "<HasErrors>true</HasErrors>" in resp.text:
                raise UserError(_("Infortisa devolvió errores: %s") % resp.text)

            # Mensaje y escritura inmediata de estado
            order.message_post(
                body=_("Pedido enviado a Infortisa. TEST=%s, BLOQUEADO=%s.<br/>Resp: %s")
                    % (test, block, resp.text[:500])
            )
            order.write({
                "infortisa_internal_ref": internal_ref or "",
                "infortisa_state": "Importing" if not test else "Test OK",
                "infortisa_sent": True,
            })

    # ========== 2) CONSULTAR ESTADO & GUARDAR IMPORTES ==========
    def action_infortisa_status(self):
        ns = {"n": "http://schemas.datacontract.org/2004/07/BackEnd.Data.Npoco.Models"}
        from_cron = self.env.context.get("infortisa_from_cron")

        for order in self:
            # Gatekeeper
            if not order.infortisa_allowed:
                continue

            if not order.infortisa_customer_ref:
                raise UserError(_("No hay CustomerReference en este pedido."))

            headers = order._get_infortisa_headers()
            url = f"{INFORTISA_BASE}/api/order/status"
            params = {"CustomerReference": order.infortisa_customer_ref}
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            previous = {
                "state": order.infortisa_state,
                "base": order.infortisa_amount_base,
                "ship": order.infortisa_amount_shipping,
                "tax": order.infortisa_amount_tax,
                "total": order.infortisa_amount_total,
                "canon": order.infortisa_amount_canon_op,
                "other": order.infortisa_amount_other_op,
                "ref": order.infortisa_transfer_ref,
                "tracking_url": order.infortisa_tracking_url,
                "tracking_number": order.infortisa_tracking_number,
                "tracking_status": order.infortisa_tracking_status,
                "tracking_detail": order.infortisa_tracking_status_detail,
            }
            order.write({"infortisa_last_response": resp.text})

            if resp.status_code != 200:
                raise UserError(_("Error estado (HTTP %s): %s") % (resp.status_code, resp.text))

            state = None
            changed_bits = []

            try:
                op = None
                if "<OrderStatusResponse" in resp.text:
                    root = ET.fromstring(resp.text)
                    op = root.find(".//n:Operation", ns)

                if op is not None:
                    def _f(tag):
                        el = op.find(f"n:{tag}", ns)
                        return float(el.text) if el is not None and el.text else 0.0

                    ship = _f("Shippingcost")
                    tax = _f("Tax")
                    total = _f("Total")
                    canon_op = _f("CanonLPI")
                    other_cost = _f("OtherCost")

                    # --- Ref. Interna (EXTxxxxx): desde InternalReference o dentro de DeliveryComment
                    internal_ref = None
                    el_int = op.find("n:InternalReference", ns)
                    if el_int is not None and el_int.text:
                        internal_ref = el_int.text.strip()

                    if not internal_ref:
                        # fallback: buscar EXT... en el DeliveryComment
                        el_dc = op.find("n:DeliveryComment", ns)
                        if el_dc is not None and el_dc.text:
                            import re as _re
                            m = _re.search(r'\bEXT\d+\b', el_dc.text)
                            if m:
                                internal_ref = m.group(0)

                    if internal_ref and internal_ref != (order.infortisa_internal_ref or ""):
                        order.infortisa_internal_ref = internal_ref
                        changed_bits.append(_("Ref. Interna Infortisa actualizada: %s") % internal_ref)
                        if not order.infortisa_sent:
                            order.infortisa_sent = True
                            changed_bits.append(_("Marcado como enviado a Infortisa."))

                    # --- Code (VR/VX/VN/...)
                    code_el = op.find("n:Code", ns)
                    code = (code_el.text.strip() if (code_el is not None and code_el.text) else "") or ""
                    if code and code != (order.infortisa_op_code or ""):
                        order.infortisa_op_code = code
                        changed_bits.append(_("Codigo operacion (Infortisa) actualizado: %s") % code)

                    # --- Referencia de transferencia (VR/..., Code, etc.)
                    transfer_ref = None
                    for tag in ("PaymentReference", "BankTransferReference", "TransferReference", "Reference", "Code"):
                        el = op.find(f"n:{tag}", ns)
                        if el is not None and el.text:
                            transfer_ref = el.text.strip()
                            break
                    if not transfer_ref:
                        el_int = op.find("n:InternalReference", ns)
                        if el_int is not None and el_int.text:
                            transfer_ref = el_int.text.strip()

                    # Guardar/propagar referencia si cambia
                    if transfer_ref and transfer_ref != order.infortisa_transfer_ref:
                        order.infortisa_transfer_ref = transfer_ref
                        changed_bits.append(_("Referencia de transferencia actualizada: %s") % transfer_ref)

                        bill = order.infortisa_vendor_bill_id
                        if bill:
                            to_write = {}
                            if getattr(bill, "payment_reference", None) != transfer_ref:
                                to_write["payment_reference"] = transfer_ref
                            if (bill.ref or "") != transfer_ref:
                                to_write["ref"] = transfer_ref
                            if to_write:
                                bill.write(to_write)
                                bill.message_post(body=_("Referencia establecida desde Infortisa: %s") % transfer_ref)

                    # --- Tracking (URL, número y estado) ---
                    trk_url_el = op.find("n:TrackingUrl", ns)
                    trk_num_el = op.find("n:TrackingNumber", ns)
                    trk_status_el = op.find("n:TrackingStatus", ns)
                    trk_status_dt_el = op.find("n:TrackingStatusDateTime", ns)
                    trk_status_det_el = op.find("n:TrackingStatusDetail", ns)

                    trk_url = (trk_url_el.text or "").strip() if trk_url_el is not None and trk_url_el.text else ""
                    trk_num = (trk_num_el.text or "").strip() if trk_num_el is not None and trk_num_el.text else ""
                    trk_status = (trk_status_el.text or "").strip() if trk_status_el is not None and trk_status_el.text else ""
                    trk_status_dt = (trk_status_dt_el.text or "").strip() if trk_status_dt_el is not None and trk_status_dt_el.text else ""
                    trk_status_det = (trk_status_det_el.text or "").strip() if trk_status_det_el is not None and trk_status_det_el.text else ""

                    # Si hay URL y es nueva/cambia -> guardar y avisar por chatter al instante
                    if trk_url and trk_url != (previous["tracking_url"] or ""):
                        order.write({
                            "infortisa_tracking_url": trk_url,
                            "infortisa_tracking_number": trk_num or order.infortisa_tracking_number,
                            "infortisa_tracking_status": trk_status or order.infortisa_tracking_status,
                            "infortisa_tracking_status_detail": trk_status_det or order.infortisa_tracking_status_detail,
                        })
                        order.message_post(
                            body=_("¡Tu pedido ha salido de almacén! 🧾<br/>"
                                   "Transportista: <b>%s</b><br/>"
                                   "Estado: <b>%s</b>%s<br/>"
                                   "%s<br/>"
                                   "Puedes seguir tu envío aquí: <a href='%s' target='_blank'>Seguimiento</a>")
                            % (
                                (op.find("n:ShippingAgent", ns).text if op.find("n:ShippingAgent", ns) is not None and op.find("n:ShippingAgent", ns).text else _("(desconocido)")),
                                trk_status or _("(sin estado)"),
                                (" — " + trk_status_dt) if trk_status_dt else "",
                                (_("Nº de seguimiento: <b>%s</b>") % trk_num) if trk_num else "",
                                trk_url,
                            )
                        )
                        changed_bits.append(_("Tracking URL detectada y enviada al chatter."))

                    else:
                        # Aunque no cambie la URL, podemos actualizar estado/número si han cambiado
                        upd = {}
                        if trk_num and trk_num != (previous["tracking_number"] or ""):
                            upd["infortisa_tracking_number"] = trk_num
                        if trk_status and trk_status != (previous["tracking_status"] or ""):
                            upd["infortisa_tracking_status"] = trk_status
                        if trk_status_det and trk_status_det != (previous["tracking_detail"] or ""):
                            upd["infortisa_tracking_status_detail"] = trk_status_det
                        if upd:
                            order.write(upd)
                            changed_bits.append(_("Información de tracking actualizada."))

                    # --- Productos -> Base propia
                    rows = []
                    for p in op.findall(".//n:Products/n:Product", ns):
                        sku = (p.find("n:SKU", ns).text if p.find("n:SKU", ns) is not None else "") or ""
                        pn = (p.find("n:Partnumber", ns).text if p.find("n:Partnumber", ns) is not None else "") or ""
                        desc = (p.find("n:ProductDescription", ns).text
                                if p.find("n:ProductDescription", ns) is not None else "") or ""
                        name = desc or pn or sku
                        qty_s = (p.find("n:Quantity", ns).text if p.find("n:Quantity", ns) is not None else "0") or "0"
                        price_wo = (p.find("n:PriceWithoutCanon", ns).text
                                    if p.find("n:PriceWithoutCanon", ns) is not None else "0") or "0"
                        canon_raw = (p.find("n:CanonLPI", ns).text if p.find("n:CanonLPI", ns) is not None else "0") or "0"

                        try:
                            qty = float(qty_s)
                        except Exception:
                            qty = 0.0
                        try:
                            price_wo_f = float(price_wo)
                        except Exception:
                            price_wo_f = 0.0
                        try:
                            canon_raw_f = float(canon_raw)
                        except Exception:
                            canon_raw_f = 0.0

                        rows.append({
                            "name": name,
                            "sku": sku,
                            "pn": pn,
                            "qty": qty,
                            "price_wo": price_wo_f,
                            "canon_raw": canon_raw_f,
                        })

                    sum_canon_units = sum(r["canon_raw"] * r["qty"] for r in rows)
                    sum_canon_as_is = sum(r["canon_raw"] for r in rows)

                    def _close(a, b):
                        return abs(a - b) <= max(0.01, 0.01 * max(a, b))

                    canon_is_unit = True
                    if _close(canon_op, sum_canon_as_is) and not _close(canon_op, sum_canon_units):
                        canon_is_unit = False

                    base_products = sum(r["price_wo"] * r["qty"] for r in rows)

                    # Guardar importes (marca cambios)
                    if base_products != previous["base"]:
                        changed_bits.append(_("Base (API) actualizada."))
                    if canon_op != previous["canon"]:
                        changed_bits.append(_("Canon LPI (operacion) actualizado."))
                    if other_cost != previous["other"]:
                        changed_bits.append(_("Otros costes (operacion) actualizados."))
                    if ship != previous["ship"]:
                        changed_bits.append(_("Portes (API) actualizados."))
                    if tax != previous["tax"]:
                        changed_bits.append(_("Impuestos (API) actualizados."))
                    if total != previous["total"]:
                        changed_bits.append(_("Total (API) actualizado."))

                    order.infortisa_amount_base = base_products
                    order.infortisa_amount_canon_op = canon_op if canon_op else (sum_canon_units if canon_is_unit else sum_canon_as_is)
                    order.infortisa_amount_other_op = other_cost
                    order.infortisa_amount_shipping = ship
                    order.infortisa_amount_tax = tax
                    order.infortisa_amount_total = total

                    st_el = op.find("n:Status", ns)
                    state = st_el.text if st_el is not None else None

                    # Render tabla
                    def _fmt(v):
                        try:
                            return f"{float(v):.2f}"
                        except Exception:
                            return "0.00"

                    prods_html = [
                        '<table class="table table-sm o_list_view">',
                        "<thead><tr>",
                        "<th>Nombre</th><th>SKU</th><th>Partnumber</th>"
                        "<th style='text-align:right'>Cantidad</th>"
                        "<th style='text-align:right'>Precio</th>"
                        "<th style='text-align:right'>Canon LPI</th>"
                        "<th style='text-align:right'>Total linea (API)</th>",
                        "</tr></thead><tbody>",
                    ]

                    canon_total_lines = 0.0
                    for r in rows:
                        canon_unit = r["canon_raw"] if canon_is_unit else (r["canon_raw"] / r["qty"] if r["qty"] else r["canon_raw"])
                        canon_line_total = canon_unit * r["qty"]
                        canon_total_lines += canon_line_total
                        line_total = r["qty"] * (r["price_wo"] + canon_unit)
                        prods_html.append(
                            "<tr>"
                            f"<td>{r['name']}</td>"
                            f"<td>{r['sku']}</td>"
                            f"<td>{r['pn']}</td>"
                            f"<td style='text-align:right'>{_fmt(r['qty'])}</td>"
                            f"<td style='text-align:right'>{_fmt(r['price_wo'])}</td>"
                            f"<td style='text-align:right'>{_fmt(canon_unit)}</td>"
                            f"<td style='text-align:right'>{_fmt(line_total)}</td>"
                            "</tr>"
                        )

                    prods_html.append("</tbody><tfoot>")
                    prods_html.append(
                        f"<tr><td colspan='6' style='text-align:right'><b>Canon LPI (operacion)</b></td>"
                        f"<td style='text-align:right'><b>{_fmt(canon_op if canon_op else canon_total_lines)}</b></td></tr>"
                    )
                    prods_html.append(
                        f"<tr><td colspan='6' style='text-align:right'><b>Otros costes (operacion)</b></td>"
                        f"<td style='text-align:right'><b>{_fmt(other_cost)}</b></td></tr>"
                    )
                    prods_html.append(
                        f"<tr><td colspan='6' style='text-align:right'>Portes (API)</td>"
                        f"<td style='text-align:right'>{_fmt(ship)}</td></tr>"
                    )
                    prods_html.append(
                        f"<tr><td colspan='6' style='text-align:right'>Impuestos (API)</td>"
                        f"<td style='text-align:right'>{_fmt(tax)}</td></tr>"
                    )
                    prods_html.append(
                        f"<tr><td colspan='6' style='text-align:right'><b>TOTAL (API)</b></td>"
                        f"<td style='text-align:right'><b>{_fmt(total)}</b></td></tr>"
                    )
                    prods_html.append("</tfoot></table>")
                    order.infortisa_products_html = "\n".join(prods_html)

                    # --- AHORA (después de importes): auto-factura y pago/lote si procede, según Code
                    try:
                        ICP = order.env["ir.config_parameter"].sudo()
                        auto_bill = ICP.get_param("infortisa.auto_create_bill") in ("True", "true", "1")

                        code_prefix_ok = code.startswith("VR/")
                        code_prefix_block = code.startswith(BLOCKED_CODE_PREFIXES)

                        if code_prefix_block:
                            # No generamos nada si está en estado no pagadero (VX/VN/VA/HR)
                            msg = _("No se genera factura/pago/XML: Code=%s indica estado no pagadero.") % (code or "(vacío)")
                            order.message_post(body=msg)
                            if order.infortisa_payment_state != "missing":
                                order.infortisa_payment_state = "missing"

                        elif code_prefix_ok:
                            # Solo en VR/ permitimos creación
                            if transfer_ref and auto_bill and not order.infortisa_vendor_bill_id:
                                order.action_infortisa_create_bill()
                                bill2 = order.infortisa_vendor_bill_id
                                if bill2:
                                    vals = {}
                                    if getattr(bill2, "payment_reference", None) != transfer_ref:
                                        vals["payment_reference"] = transfer_ref
                                    if (bill2.ref or "") != transfer_ref:
                                        vals["ref"] = transfer_ref
                                    if vals:
                                        bill2.write(vals)
                                        bill2.message_post(body=_("Factura creada automáticamente y referenciada: %s") % transfer_ref)

                            if transfer_ref and order.infortisa_vendor_bill_id and not order.infortisa_vendor_payment_id:
                                order._create_vendor_payment_and_xml()

                        else:
                            # Code vacío o aún no devuelto: solo avisamos en ejecución manual (no cron)
                            if not from_cron and not code:
                                order.message_post(body=_("Code no disponible aún; se pospone la generación de factura/pago/XML."))

                    except Exception as e:
                        order.message_post(body=_("Error al procesar pago/lote tras recibir referencia: %s") % e)

                elif "State of Order:" in resp.text:
                    state = resp.text.split("State of Order:")[1].split("<")[0].strip()
                else:
                    state = "Desconocido"

            except Exception as parse_err:
                _logger.exception("No se pudo parsear OrderStatusResponse: %s", parse_err)
                state = state or "Desconocido"

            if state != previous["state"]:
                changed_bits.append(_("Estado Infortisa actualizado: %s") % (state or ""))

            order.write({"infortisa_state": state or ""})

            # Mensajería: manual = detallado; cron = solo si hay cambios
            if from_cron:
                if changed_bits:
                    order.message_post(body="<br/>".join(changed_bits))
            else:
                order.message_post(
                    body=_("Estado Infortisa actualizado: <b>%s</b><br/>Resp: %s")
                    % (state or "", (resp.text or "")[:500])
                )

    # ========== 3) BLOQUEAR / DESBLOQUEAR / ANULAR ==========
    def _action_infortisa_block_cancel(self, cancel=False, block=False):
        for order in self:
            if not order.infortisa_customer_ref:
                raise UserError(_("No hay CustomerReference en este pedido."))
            headers = order._get_infortisa_headers()
            url = f"{INFORTISA_BASE}/api/order/blockorder"
            xml = f"""<?xml version="1.0" encoding="utf-16"?>
            <BlockOrder xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <CustomerReference>{order.infortisa_customer_ref}</CustomerReference>
            <BlockOrder>{str(block).lower()}</BlockOrder>
            <CancelOrder>{str(cancel).lower()}</CancelOrder>
            </BlockOrder>
            """
            resp = requests.post(url, headers=headers, data=xml.encode("utf-16"), timeout=60)
            order.write({"infortisa_last_response": resp.text})
            if resp.status_code != 200:
                raise UserError(_("Error bloquear/anular (HTTP %s): %s") % (resp.status_code, resp.text))
            order.message_post(
                body=_("Acción Infortisa ejecutada. Cancel=%s Block=%s<br/>Resp: %s")
                % (cancel, block, (resp.text or "")[:500])
            )
            order.action_infortisa_status()

    def action_infortisa_block(self):
        self._action_infortisa_block_cancel(cancel=False, block=True)

    def action_infortisa_unblock(self):
        self._action_infortisa_block_cancel(cancel=False, block=False)

    def action_infortisa_cancel(self):
        self._action_infortisa_block_cancel(cancel=True, block=False)

    # ========== 4) CRON: poll estado ==========
    @api.model
    def cron_infortisa_poll_status(self):
        # sudo para evitar problemas de permisos
        orders = self.sudo().search([
            ("infortisa_sent", "=", True),
            ("infortisa_allowed", "=", True),
        ])
        for order in orders:
            try:
                # Refresca estado (el chatter desde cron solo escribe si hay cambios)
                order.with_context(infortisa_from_cron=True).action_infortisa_status()
                # Auto: factura (si activado) + pago + XML
                order._auto_make_payment_if_ready()
            except Exception as e:
                _logger.exception("Poll estado Infortisa falló para SO %s: %s", order.name, e)
                order.message_post(body=_("Cron Infortisa: error al actualizar o procesar: %s") % e)

    # ========== 5) AUTO-ENVÍO cuando está pagado ==========
    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            try:
                # Gatekeeper: sólo si el pedido está permitido por proveedor
                if order.infortisa_allowed and not order.infortisa_sent:
                    order.action_infortisa_send(block=None, test=None)
            except Exception as e:
                order.message_post(body=_("Fallo al enviar a Infortisa automáticamente: %s") % e)
        return res

    # ========== 6) CREAR FACTURA DE PROVEEDOR DESDE IMPORTES API ==========
    def action_infortisa_create_bill(self):
        self.ensure_one()

        if self.infortisa_vendor_bill_id:
            raise UserError(_("Ya existe una factura de proveedor enlazada a este pedido."))

        ICP = self.env["ir.config_parameter"].sudo()

        vendor_id = ICP.get_param("infortisa.vendor_id")
        journal_id = ICP.get_param("infortisa.journal_id")
        product_purchase_id = ICP.get_param("infortisa.product_purchase_id")
        product_shipping_id = ICP.get_param("infortisa.product_shipping_id")

        partner = self.env["res.partner"].browse(int(vendor_id or 0)) if vendor_id else False
        if not partner:
            raise UserError(_("Configura el 'Proveedor Infortisa' en Ajustes > Infortisa."))

        journal = False
        if journal_id:
            journal = self.env["account.journal"].browse(int(journal_id))
        if not journal or not journal.exists():
            journal = self.env["account.journal"].search([("type", "=", "purchase")], limit=1)
        if not journal:
            raise UserError(_("No se ha encontrado un diario de compras. Configúralo en Ajustes > Infortisa."))

        lines = []

        def _line_from_product(prod_id, name, qty, price):
            vals = {"name": name, "quantity": qty, "price_unit": price}
            if prod_id:
                prod = self.env["product.product"].browse(int(prod_id))
                if prod and prod.exists():
                    vals.update({"product_id": prod.id, "product_uom_id": prod.uom_id.id})
            return (0, 0, vals)

        if self.infortisa_amount_base and self.infortisa_amount_base > 0:
            lines.append(_line_from_product(
                product_purchase_id, _("Compra Infortisa %s - Base API") % (self.name,), 1.0, self.infortisa_amount_base
            ))
        if self.infortisa_amount_shipping and self.infortisa_amount_shipping > 0:
            lines.append(_line_from_product(
                product_shipping_id, _("Portes Infortisa %s - API") % (self.name,), 1.0, self.infortisa_amount_shipping
            ))

        if not lines:
            raise UserError(_("No hay importes de API para facturar. Pulsa 'Actualizar estado' antes."))

        bill = self.env["account.move"].create({
            "move_type": "in_invoice",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_origin": self.name,
            "invoice_date": fields.Date.context_today(self),
            "currency_id": self.currency_id.id,
            "invoice_payment_term_id": partner.property_supplier_payment_term_id.id or False,
            "invoice_line_ids": lines,
        })
        vals_ref = {}
        if self.infortisa_transfer_ref:
            vals_ref["payment_reference"] = self.infortisa_transfer_ref  # Referencia de factura
            vals_ref["ref"] = self.infortisa_transfer_ref                # Referencia interna
        if vals_ref:
            bill.write(vals_ref)
        bill.message_post(body=_("Factura generada desde pedido %s (Infortisa).") % self.name)
        self.infortisa_vendor_bill_id = bill.id

        action = self.env.ref("account.action_move_in_invoice_type").read()[0]
        action["views"] = [(self.env.ref("account.view_move_form").id, "form")]
        action["res_id"] = bill.id
        return action

 
 
 
 
