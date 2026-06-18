# Bot Economia G3NESYS Albion 1.0

Bot modular para Discord.py, SQLite y Railway.

## Que incluye

- Panel de actividades con plantillas, inscripciones por arma/rol, aviso, inicio, check de asistencia, finalizacion y generar reparto.
- Penalizacion automatica con 3 multas pendientes, 3 inasistencias seguidas o 10 inasistencias acumuladas.
- Multas manuales y automaticas con notificaciones por DM.
- Banco con saldo disponible, retenido y decomisado.
- Pago de multas con saldo disponible o retenido. El saldo retenido se usa solo para multas.
- Cobros para MIEMBRO G3NESYS e INVITADO. El saldo no se descuenta al solicitar, solo cuando admin liquida.
- Panel administrativo con tesoreria, ingresos, egresos, depositos admin, repartos, cobros, historial, rankings, reportes y auditoria.
- Backups automaticos de SQLite.
- Paneles con emojis, botones coloreados y respuestas por DM cuando es posible para no saturar canales.

## Instalacion local

Opcion rapida en Windows:

1. Ejecuta doble clic en `iniciar_bot_local.bat`.
2. Si es la primera vez, el archivo creara `.env`.
3. Abre `.env`, coloca `DISCORD_TOKEN`, guarda y vuelve a ejecutar `iniciar_bot_local.bat`.

Opcion manual:

1. Crea un entorno de Python 3.11 o superior.
2. Instala dependencias:

```bash
pip install -r requirements.txt
```

3. Copia `.env.example` como `.env`.
4. Coloca el token del bot en `DISCORD_TOKEN`.
5. Ejecuta:

```bash
python main.py
```

## Variables para Railway

- `DISCORD_TOKEN`: token del bot.
- `DATABASE_PATH`: por defecto `data/g3nesys.sqlite3`.
- `COMMAND_PREFIX`: por defecto `!`.
- `BACKUP_DIR`: por defecto `data/backups`.
- `BACKUP_EVERY_MINUTES`: por defecto `360`.

## Intents del bot

En Discord Developer Portal activa:

- Server Members Intent.
- Message Content Intent.
- Presence no es necesario.

El bot necesita permisos para:

- Enviar mensajes.
- Enviar embeds.
- Usar botones.
- Leer historial.
- Gestionar mensajes si quieres que borre el comando usado para publicar paneles.

## Configuracion inicial dentro de Discord

1. Da permiso admin temporal al usuario que configurara el bot.
2. Publica canales:

```text
!canal_pings_set
!canal_admin_set
!canal_cobros_set
!canal_multas_set
!canal_historial_set
!canal_repartos_set
```

Cada comando se ejecuta en el canal que quieres configurar.

3. Autoriza roles y callers:

```text
!admin_role_set @RolAdmin
!caller_set @Usuario
```

4. Configura economia opcional:

```text
!economia_set absence_fine_enabled 1
!economia_set absence_fine_amount 200000
!economia_set minimum_withdrawal 0
!economia_set transfer_fee_percent 3
```

5. Publica paneles:

```text
!panel_pings
!panel_banco
!panel_admin
```

## Reglas importantes implementadas

- Los paneles quedan fijos y las respuestas de botones son privadas cuando Discord lo permite.
- El caller crea actividades y genera reparto, pero no deposita saldos.
- En actividades, "Mandar check" envia el check por DM y "Verificar asistencia" manda al caller la lista de quienes dieron check.
- Al generar reparto, el caller recibe por DM la lista de participantes confirmados con 100% de participacion por defecto, puede editar porcentajes y luego enviarlo a revision con boton.
- Los repartos llegan a admins con botones para aprobar, rechazar, pedir correccion y ver detalle.
- El admin aprueba repartos y liquida cobros.
- Si el usuario confirma asistencia pero no esta en el canal de voz configurado, queda ausente.
- La multa automatica por inasistencia solo se genera para ausentes y solo si esta activada.
- Con 3 multas pendientes el usuario entra en penalizacion de actividades.
- Invitados pueden recibir pagos y solicitar cobros, pero no pueden transferir.
- Las transferencias son solo MIEMBRO G3NESYS a MIEMBRO G3NESYS, con comision del 3% por defecto para Tesoreria.
- Si Tesoreria no tiene saldo suficiente, no se permite hacer depositos administrativos ni egresos.

## Comandos principales

```text
!ayuda_g3n
!panel_pings
!panel_banco
!panel_admin
!caller_set @usuario
!caller_remove @usuario
!penalizaciones
!penalizacion_remove @usuario
!crear_multa @usuario monto motivo
!cancelar_multa MULTA-000001 motivo
!mis_multas
!pagar_multa MULTA-000001
!saldo
!estado_cuenta
!transferir @usuario monto
!cobrar monto motivo
!tesoreria
!registrar_ingreso monto categoria descripcion
!registrar_egreso monto categoria descripcion
!depositar_usuario @usuario monto disponible motivo
!aprobar_cobro COBRO-000001
!rechazar_cobro COBRO-000001 motivo
!liquidar_cobro COBRO-000001 monto
!aprobar_reparto REP-000001
!rechazar_reparto REP-000001 motivo
!corregir_reparto REP-000001 motivo
!reparto_participantes REP-000001
!reparto_participacion REP-000001 @usuario 10
!reparto_agregar REP-000001 @usuario 100
!reparto_quitar REP-000001 @usuario
!reporte_excel
```

## Formato para roles de plantillas

En el modal de crear plantilla o crear actividad:

```text
🌾 | Falce | 2
🔮 | Prisma | 2
🛡️ | Tanque | 1
```

El orden es `emoji | nombre del rol o arma | cantidad requerida`. El emoji es
opcional y tambien puedes pegar un emoji personalizado del servidor. El ping
mostrara el avance como `Falce [0/2]` y bloqueara el boton al llegar a `[2/2]`.

Cuando el caller mande el check, **Aqui estoy** solo confirmara a usuarios
conectados al canal de voz configurado. Si la actividad no tiene un canal
especifico, el usuario debe estar conectado a cualquier canal de voz.
