# infortisa_orders (Odoo 18)
Módulo para enviar pedidos a Infortisa, consultar estado, y creación opcional de factura y pago ISO20022.

## Instalación
- Copiar en `addons_path`
- Actualizar lista de módulos y activar `infortisa_orders`.

## Configuración
Ajustes > Infortisa:
- API Key
- Modo TEST (opcional)
- Proveedor, productos de coste y portes, diario compras
- Diario banco y auto-factura/pago (opcional)

## Uso
En el pedido de venta:
- Botón *Enviar a Infortisa*
- *Actualizar estado* para traer totales/ref. transferencia y (si procede) crear factura/pago.
