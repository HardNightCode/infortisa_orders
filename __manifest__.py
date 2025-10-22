{
    "name": "Infortisa Orders",
    "summary": "Envío de pedidos a Infortisa, tracking de estado y factura proveedor por API",
    "version": "18.0.7.7",
    "author": "Nexus Antonio",
    "website": "",
    "category": "Sales",
    "license": "LGPL-3",
    "installable": True,
    "application": False,
    "depends": [
        "sale_management",
        "account",
        "website_sale",
        "delivery",
        "account_batch_payment",
	"account_iso20022"
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/sale_order_views.xml",
        "data/ir_cron.xml",
        "views/res_config_settings_views.xml",
	"views/raw_wizard_views.xml",   # <-- añade esta línea
    ],
}

