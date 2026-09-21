# Backend - Sistema IoT para Monitoreo de Establecimientos

Backend en la nube para el sistema IoT desarrollado como parte del proyecto de carrera de Ingeniería en Computación.

Este repositorio contiene exclusivamente el servicio backend encargado de recibir, validar, procesar y almacenar la información enviada desde el gateway local del sistema.

## Descripción

El backend forma parte de una arquitectura híbrida Local/Nube.

Los dispositivos ESP32 realizan la adquisición y procesamiento inicial de datos dentro de una red IoT local. Estos dispositivos no se comunican directamente con Internet.

La Raspberry Pi Zero 2 W funciona como gateway de la red, concentrando los eventos provenientes de los dispositivos mediante MQTT y enviando posteriormente la información hacia este backend mediante HTTPS.

Flujo general:

ESP32 / Sensores
        ↓
MQTT
        ↓
Raspberry Pi Zero 2 W
        ↓
HTTPS / REST API
        ↓
FastAPI Backend
        ↓
Base de datos / Servicios Cloud
        ↓
Frontend

Este repositorio corresponde únicamente al componente:

Raspberry Pi → **Backend FastAPI** → Base de datos

---

## Objetivo

El objetivo del backend es proporcionar una API centralizada que permita:

- Recibir información proveniente del gateway Raspberry Pi.
- Validar los datos recibidos.
- Registrar eventos y mediciones.
- Consultar la información almacenada.
- Proporcionar información al frontend.
- Mantener separados los dispositivos IoT de los servicios y credenciales de nube.
- Permitir posteriormente la integración de mecanismos de autenticación, monitoreo y análisis.

---

## Arquitectura

La arquitectura seleccionada para el proyecto utiliza un modelo híbrido Local/Nube.

Los dispositivos IoT se encuentran aislados de Internet y se comunican únicamente con un broker MQTT ejecutado en una Raspberry Pi Zero 2 W.

La Raspberry Pi realiza funciones de:

- Broker MQTT mediante Mosquitto.
- Gateway entre la red IoT y la nube.
- Validación inicial de mensajes.
- Almacenamiento temporal mediante SQLite.
- Sincronización de eventos pendientes.
- Agrupación de registros antes de enviarlos al backend.

El backend recibe los datos enviados por el gateway y los almacena en los servicios correspondientes.

```text
┌──────────────────────┐
│       ESP32          │
│ Sensores / Actuadores│
└──────────┬───────────┘
           │
           │ MQTT
           ▼
┌──────────────────────┐
│ Raspberry Pi Zero 2W │
│ Mosquitto + SQLite   │
│ Gateway / Edge       │
└──────────┬───────────┘
           │
           │ HTTPS / TLS
           ▼
┌──────────────────────┐
│       FastAPI        │
│       Backend        │
│       Railway        │
└──────────┬───────────┘
           │
     ┌─────┴──────┐
     ▼            ▼
 PostgreSQL    InfluxDB
 Supabase      Cloud
