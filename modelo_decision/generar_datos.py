"""Genera los ejemplos inventados para entrenar el modelo de decisión de AIDM.

Tarea «estado» (fallos 36 y 68). Cada ejemplo es un turno del discovery: el campo por el que pregunta el asistente,
su pregunta y la respuesta del cliente. La etiqueta dice qué hace la respuesta con ese campo:
    dato         da el dato, aunque sea aproximado o negativo («no tenemos», «ninguno», «solo Excel»)
    no_lo_sabe   dice que no lo sabe, sin dar ningún valor
    no_aplica    dice que ese tema no aplica a su proyecto o que no lo necesita
    no_responde  no contesta a lo preguntado: cambia de tema, da un dato de otro campo o lo deja para luego
    vaga         habla del dato sin concretar nada («bastantes», «lo normal», «depende»): se vuelve a preguntar

    
Nada sale de clientes reales. Las respuestas de prueba (las listas *_PRUEBA) no aparecen en el entrenamiento,
así que la prueba mide si el modelo generaliza a frases que no ha visto.

Uso, desde la carpeta del proyecto:
    python modelo_decision/generar_datos.py
Escribe modelo_decision/datos/entrenamiento.jsonl y modelo_decision/datos/prueba.jsonl en el formato de
Unsloth (state, questions, gold).
"""
import json
import random
import sys
import unicodedata
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from decisiones import PREGUNTA_ESTADO, texto_estado  # noqa: E402  la misma pregunta y el mismo texto que AIDM

SALIDA = RAIZ / "modelo_decision" / "datos"
SEMILLA = 3407

# --------------------------------------------------------------------------------------------------------
# Campos. Para cada uno: grupo (para no usar como «otro dato» una respuesta de un campo parecido), tema (para
# las respuestas genéricas), preguntas y respuestas con dato, en español e inglés, y si admite «no lo sé» y
# «no aplica». Las listas "prueba" quedan fuera del entrenamiento.
# --------------------------------------------------------------------------------------------------------
CAMPOS = {
    "nombre_del_cliente": {
        "grupo": "empresa", "no_lo_sabe": False, "no_aplica": False, "vaga": False,
        "tema": ("el nombre de la empresa", "the company name"),
        "preguntas": {
            "es": ["¿Cómo se llama tu empresa?", "¿Para qué empresa sería el proyecto?",
                   "Para empezar, ¿me dices el nombre de la empresa?"],
            "en": ["What's the name of your company?", "Which company is this project for?"],
        },
        "datos": {
            "es": ["Somos Transportes Galaicos.", "Logística Ebro, S.L.", "La empresa se llama Panadería Muñoz.",
                   "Trabajo en Clínicas Avanza.", "Grupo Hostelero Sur.", "Somos Ferretería López e Hijos.",
                   "Construcciones Arlanza.", "Viveros del Norte, una cooperativa."],
            "en": ["We're Northwind Logistics.", "Brightside Retail Ltd.", "The company is called Kestrel Foods.",
                   "Harbor Clinics."],
        },
        "prueba": {
            "es": ["Me llamo Ana y llamo de parte de Bodegas Ribera.", "Seguros Atalaya."],
            "en": ["I'm calling from Bluepeak Energy."],
        },
        "no_responde_propio": {
            "es": ["Prefiero no decir el nombre todavía.", "¿Hace falta el nombre para esto?"],
            "en": ["I'd rather not give the name yet."],
        },
    },
    "tamaño_empresa": {
        "grupo": "empresa", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("cuántos empleados sois", "the headcount"),
        "preguntas": {
            "es": ["¿Cuántas personas trabajan en la empresa, más o menos?", "¿Qué tamaño tiene la empresa?",
                   "¿Sois una pyme o una empresa grande?"],
            "en": ["How many people work at the company?", "How big is the company?"],
        },
        "datos": {
            "es": ["Unos 250 empleados.", "Somos una pyme de 40 personas.",
                   "Alrededor de 1.200 trabajadores entre todas las sedes.", "Somos 12.",
                   "Una startup de 8 personas.", "Más de 5.000 empleados en España.", "Pequeña, unas 20 personas.",
                   "Una empresa mediana, entre 100 y 150."],
            "en": ["Around 300 employees.", "We're a small business, about 25 people.", "Roughly 2,000 staff.",
                   "A startup of 10."],
        },
        "prueba": {
            "es": ["Rondamos los 600 empleados.", "Somos cuatro gatos, 6 personas."],
            "en": ["Somewhere between 80 and 100 people."],
        },
    },
    "sector": {
        "grupo": "empresa", "no_lo_sabe": False, "no_aplica": False, "vaga": False,
        "tema": ("el sector", "your industry"),
        "preguntas": {
            "es": ["¿A qué sector se dedica la empresa?", "¿En qué sector trabajáis?",
                   "¿Cuál es vuestra actividad principal?"],
            "en": ["What industry are you in?", "What does your company do?"],
        },
        "datos": {
            "es": ["Transporte de mercancías por carretera.", "Retail, tenemos tiendas de ropa.",
                   "Somos un laboratorio farmacéutico.", "Distribución de alimentación.", "Banca y seguros.",
                   "Fabricamos piezas para automoción.", "Somos una cadena de hoteles.", "Energía, sobre todo renovables."],
            "en": ["Logistics and freight.", "Retail, mostly online.", "Healthcare, we run clinics.", "Manufacturing."],
        },
        "prueba": {
            "es": ["Nos dedicamos a la gestión de residuos.", "Educación: tenemos tres colegios."],
            "en": ["We're an insurance broker."],
        },
    },
    "tiene_equipo_datos": {
        "grupo": "equipo", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("lo del equipo de datos", "the data team"),
        "preguntas": {
            "es": ["¿Tenéis equipo de datos propio?", "¿Hay alguien en la empresa que se dedique a datos o analítica?",
                   "¿Contáis con un equipo de datos o trabajáis con proveedores?"],
            "en": ["Do you have an in-house data team?", "Is there anyone working on data or analytics?"],
        },
        "datos": {
            "es": ["No, no tenemos equipo de datos.", "No hay nadie dedicado a datos.",
                   "Tenemos dos analistas que hacen informes en Excel.",
                   "Sí, un equipo de 5 personas con un ingeniero de datos.", "No, dependemos de un proveedor externo.",
                   "Hay una persona de IT que hace consultas SQL de vez en cuando.",
                   "Ninguno, todo lo hace la consultora.", "Sí, tres científicos de datos y dos de BI."],
            "en": ["No, we don't have a data team.", "Two analysts who mostly use Excel.",
                   "Yes, a team of four, including a data engineer.", "Nobody, we rely on an external vendor."],
        },
        "prueba": {
            "es": ["Qué va, aquí nadie sabe de datos.", "Tenemos un becario que lleva los informes."],
            "en": ["Not really, just one person in finance who builds reports."],
        },
    },
    "tipo_de_servicio": {
        "grupo": "equipo", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("qué servicio queréis", "the kind of service"),
        "preguntas": {
            "es": ["¿Qué esperáis de Inetum: que lo implementemos, consultoría, formación…?",
                   "¿Qué tipo de servicio necesitáis?", "¿Queréis que lo desarrollemos nosotros o solo asesoramiento?"],
            "en": ["What kind of service do you need from us?", "Should we build it, advise you, or train your team?"],
        },
        "datos": {
            "es": ["Que lo implementéis vosotros y luego nos forméis.",
                   "Solo consultoría, el desarrollo lo hacemos nosotros.",
                   "Implementación completa y mantenimiento después.", "Formación para el equipo, nada más.",
                   "Que nos ayudéis con el diseño de la arquitectura.", "Llave en mano.",
                   "Un piloto primero y luego ya veremos.", "Desarrollo y soporte durante un año."],
            "en": ["Full implementation, then training.", "Just consulting, we'll build it.",
                   "End-to-end delivery plus support.", "Training for our analysts."],
        },
        "prueba": {
            "es": ["Necesitamos que nos lo montéis entero, no tenemos gente.", "Asesoramiento para elegir herramientas."],
            "en": ["Design help only."],
        },
    },
    "objetivo_del_proyecto": {
        "grupo": "proyecto", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("el objetivo", "the goal"),
        "preguntas": {
            "es": ["¿Cuál es el objetivo principal del proyecto?", "¿Qué queréis conseguir con este proyecto?",
                   "¿Qué problema queréis resolver?"],
            "en": ["What's the main goal of the project?", "What problem are you trying to solve?"],
        },
        "datos": {
            "es": ["Predecir la demanda para no quedarnos sin stock.", "Tener un cuadro de mando con las ventas diarias.",
                   "Optimizar las rutas de reparto.", "Reducir las devoluciones.",
                   "Automatizar los informes mensuales que hacemos a mano.", "Detectar fraude en las reclamaciones.",
                   "Centralizar los datos que ahora están en varios Excel.", "Saber qué clientes se van a dar de baja."],
            "en": ["Forecast demand.", "A dashboard for daily sales.", "Optimise delivery routes.",
                   "Predict customer churn."],
        },
        "prueba": {
            "es": ["Que dirección vea los KPIs sin pedírselos a nadie.", "Anticipar averías en la maquinaria."],
            "en": ["Stop building monthly reports by hand."],
        },
    },
    "modelo_de_despliegue": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("dónde se desplegaría", "where it would run"),
        "preguntas": {
            "es": ["¿Preferís que la solución esté en la nube, en vuestros servidores o un modelo híbrido?",
                   "¿Dónde viviría la solución: cloud, on-premises o híbrido?",
                   "¿Tenéis preferencia por cloud o por servidores propios?"],
            "en": ["Would you prefer cloud, on-premises or hybrid?", "Where should the solution run?"],
        },
        "datos": {
            "es": ["En la nube.", "On-premises, no queremos datos fuera.",
                   "Híbrido: los datos sensibles en local y el resto en cloud.", "Todo en cloud, ya tenemos Azure.",
                   "En nuestros servidores.", "Nos da igual, lo que sea más barato.", "Preferimos cloud.",
                   "Ahora todo está en local, pero queremos ir a la nube."],
            "en": ["Cloud.", "On-premises.", "Hybrid.", "Whatever is cheapest, we don't mind."],
        },
        "prueba": {
            "es": ["Nada de nube, todo en casa.", "Una mezcla, como ahora."],
            "en": ["We'd like to keep it in our own data centre."],
        },
    },
    "cloud_provider": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("lo del proveedor de nube", "the cloud provider"),
        "preguntas": {
            "es": ["¿Usáis algún proveedor de nube, como Azure, AWS o Google Cloud?", "¿Qué nube preferís?",
                   "¿Tenéis ya contrato con algún proveedor cloud?"],
            "en": ["Which cloud provider do you use or prefer?", "Are you on Azure, AWS or Google Cloud?"],
        },
        "datos": {
            "es": ["Azure, por Microsoft 365.", "AWS.", "Google Cloud.", "No usamos ninguna nube.",
                   "Tenemos algo en Azure pero poco.", "Oracle Cloud, por el ERP.", "Ninguno todavía.",
                   "Preferiríamos AWS, pero no tenemos nada contratado."],
            "en": ["Azure.", "AWS, mostly S3.", "We don't use any cloud.", "Google Cloud."],
        },
        "prueba": {
            "es": ["Estamos con Microsoft para todo.", "De momento ninguno, todo está en local."],
            "en": ["Nothing yet."],
        },
        "no_aplica_propio": {
            "es": ["No aplica, la solución va a ir en nuestros servidores."],
            "en": ["Not applicable, it'll be on-premises."],
        },
    },
    "tipo_de_arquitectura": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("el tipo de arquitectura", "the architecture"),
        "preguntas": {
            "es": ["¿Tenéis preferencia de arquitectura: data warehouse, data lake, lakehouse…?",
                   "¿Qué estilo de arquitectura os encaja más?",
                   "¿Habéis pensado en un data warehouse o en un data lake?"],
            "en": ["Do you have a preferred architecture: data warehouse, data lake or lakehouse?",
                   "What kind of architecture are you thinking of?"],
        },
        "datos": {
            "es": ["Un data warehouse, que es lo que conocemos.", "Lakehouse.", "Un data lake para guardar todo en bruto.",
                   "Ahora tenemos un SQL Server y queremos seguir con algo parecido.",
                   "Algo sencillo, un data warehouse pequeño.", "Data lake con una capa de BI encima.",
                   "Un DWH en la nube.", "Algo tipo lakehouse en Databricks."],
            "en": ["A data warehouse.", "Lakehouse.", "A data lake.", "Something simple, like a small warehouse."],
        },
        "prueba": {
            "es": ["Un almacén de datos clásico.", "Nos han hablado bien del lakehouse."],
            "en": ["A cloud data warehouse."],
        },
        "no_lo_sabe_propio": {
            "es": ["No sé qué es un lakehouse.", "No sé qué diferencia hay entre esas cosas."],
            "en": ["I don't know what a lakehouse is."],
        },
    },
    "herramienta_de_visualizacion": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("la herramienta de informes", "the BI tool"),
        "preguntas": {
            "es": ["¿Usáis alguna herramienta de visualización, como Power BI o Tableau?",
                   "¿Con qué herramienta hacéis hoy los informes?", "¿Tenéis licencias de alguna herramienta de BI?"],
            "en": ["Do you use a BI tool like Power BI or Tableau?", "Which reporting tool do you use?"],
        },
        "datos": {
            "es": ["Power BI, tenemos 10 licencias Pro.", "Tableau.", "Ninguna, todo en Excel.",
                   "Looker Studio, el gratuito.", "Qlik, aunque lo usamos poco.", "No tenemos ninguna herramienta.",
                   "Power BI Desktop, sin licencias de pago.", "Metabase en un servidor propio."],
            "en": ["Power BI, about 20 Pro licences.", "Tableau.", "None, we use Excel.", "Looker Studio."],
        },
        "prueba": {
            "es": ["Solo Excel y tablas dinámicas.", "Tenemos Power BI con licencias Premium."],
            "en": ["Qlik Sense."],
        },
        "no_aplica_propio": {
            "es": ["No necesitamos dashboards, solo una API.",
                   "No va a haber visualización, los datos van a otro sistema."],
            "en": ["We won't need dashboards, just an API."],
        },
    },
    "enfoque_ai": {
        "grupo": "proyecto", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("lo de la IA", "the AI part"),
        "preguntas": {
            "es": ["¿Qué papel queréis que tenga la IA en el proyecto?",
                   "¿Habéis pensado en usar inteligencia artificial, por ejemplo modelos predictivos o generativos?",
                   "¿Qué tipo de IA os interesa?"],
            "en": ["What role should AI play in the project?",
                   "Are you thinking of predictive models, generative AI, or neither?"],
        },
        "datos": {
            "es": ["Modelos predictivos para la demanda.", "Un asistente que responda preguntas sobre nuestros documentos.",
                   "Clasificar automáticamente las incidencias.", "Detección de anomalías en los sensores.",
                   "IA generativa para resumir informes.", "Algo de machine learning para segmentar clientes.",
                   "Un chatbot para atención al cliente.", "Visión artificial para el control de calidad."],
            "en": ["Predictive models for demand.", "A chatbot over our documents.", "Anomaly detection.",
                   "Customer segmentation."],
        },
        "prueba": {
            "es": ["Que nos avise antes de que un cliente se vaya.", "Predecir cuándo va a fallar una máquina."],
            "en": ["Generative AI to draft reports."],
        },
        "no_aplica_propio": {
            "es": ["No queremos IA, solo informes.", "En este proyecto no hace falta IA."],
            "en": ["No AI for now, just reporting."],
        },
    },
    "integration_scope": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("lo de la integración", "the integration"),
        "preguntas": {
            "es": ["¿Qué sistemas habría que integrar?", "¿De cuántas fuentes vienen los datos que hay que juntar?",
                   "¿Qué nivel de integración necesitáis?"],
            "en": ["Which systems would need to be integrated?", "How many data sources need to be connected?"],
        },
        "datos": {
            "es": ["El ERP y el CRM.", "SAP y unos Excel que manda cada tienda.", "Solo la base de datos de ventas.",
                   "Unas 6 fuentes: ERP, CRM, web, almacén y dos APIs.",
                   "Hay que integrar los datos de los sensores con el ERP.", "Salesforce y nuestra base de datos de pedidos.",
                   "Todo está en un solo sistema, no hay que integrar nada más.",
                   "Los ficheros que nos llegan por FTP de los proveedores."],
            "en": ["The ERP and the CRM.", "SAP plus some spreadsheets.", "Just the sales database.",
                   "About five sources."],
        },
        "prueba": {
            "es": ["Navision y la tienda online.", "Un único Oracle."],
            "en": ["Salesforce and our order system."],
        },
    },
    "volumen_de_datos": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("el volumen de datos", "the data volume"),
        "preguntas": {
            "es": ["¿Qué volumen de datos manejáis, aproximadamente?", "¿Cuántos datos generáis al día o al mes?",
                   "¿Tenéis idea del volumen de datos: gigas, teras…?"],
            "en": ["Roughly how much data do you handle?", "How much data do you generate per day or month?"],
        },
        "datos": {
            "es": ["Unos 2 TB en total.", "Poca cosa, unos cuantos gigas.", "Unos 50.000 pedidos al mes.",
                   "Cada camión manda una posición cada minuto y son 300 camiones.", "Alrededor de 500 GB al año.",
                   "Millones de registros, pero no sé cuántos gigas son.", "Un par de Excel de 20 MB al mes.",
                   "10 TB de imágenes."],
            "en": ["About 2 TB.", "A few gigabytes.", "Around 50,000 orders a month.", "Roughly 500 GB a year."],
        },
        "prueba": {
            "es": ["Generamos unos 5 GB diarios.", "Pocos datos, cabe todo en un Excel grande."],
            "en": ["Millions of rows, not sure how many GB."],
        },
    },
    "madurez_analitica_actual": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("cómo trabajáis hoy con los datos", "how you work with data today"),
        "preguntas": {
            "es": ["¿Cómo trabajáis hoy con los datos: tenéis data warehouse, BI o solo Excel?",
                   "¿Qué herramientas de análisis usáis ahora?", "¿En qué punto estáis con la analítica?"],
            "en": ["How do you work with data today?", "Do you have a data warehouse or BI, or just Excel?"],
        },
        "datos": {
            "es": ["Solo Excel.", "Tenemos un data warehouse antiguo en SQL Server y Power BI.",
                   "Nada, los datos están en el ERP y nadie los explota.", "Informes que saca IT del ERP cada mes.",
                   "Algún dashboard suelto en Power BI.", "Ninguna herramienta, todo a mano.",
                   "Un DWH en Snowflake y modelos en Python.", "Hojas de cálculo compartidas en SharePoint."],
            "en": ["Just Excel.", "An old SQL Server warehouse and Power BI.", "Nothing, the data sits in the ERP.",
                   "Some ad-hoc dashboards."],
        },
        "prueba": {
            "es": ["Estamos empezando: unos Excel y poco más.", "Tenemos BI, pero cada departamento hace lo suyo."],
            "en": ["Monthly reports pulled from the ERP by IT."],
        },
    },
    "tamano_equipo_tecnico": {
        "grupo": "equipo", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("quién lo mantendría", "who would maintain it"),
        "preguntas": {
            "es": ["¿Quién mantendría la solución cuando esté entregada? ¿Cuántas personas son y qué saben hacer?",
                   "¿Qué equipo técnico tenéis para operar la solución?",
                   "¿Cuántas personas de IT tenéis y qué conocimientos tienen?"],
            "en": ["Who would maintain the solution after delivery?", "How big is your IT team and what do they know?"],
        },
        "datos": {
            "es": ["IT son 5 personas y una sabe Python.", "Dos de sistemas, ninguno sabe de datos.",
                   "Nadie, querríamos que lo mantuvierais vosotros.", "Un equipo de 10 desarrolladores.",
                   "Tres personas que saben SQL.", "Solo el responsable de IT.", "No tenemos equipo técnico.",
                   "Ocho en IT, dos con algo de Azure."],
            "en": ["Five people in IT, one knows Python.", "Nobody, we'd want you to maintain it.", "Two sysadmins.",
                   "A team of ten developers."],
        },
        "prueba": {
            "es": ["Somos dos en informática y vamos a tope.", "Un proveedor externo nos lleva los sistemas."],
            "en": ["Just our IT manager."],
        },
    },
    "stack_tecnologico_actual": {
        "grupo": "plataforma", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("los sistemas que usáis", "your current systems"),
        "preguntas": {
            "es": ["¿Qué tecnologías usáis hoy: ERP, CRM, bases de datos, nube…?", "¿Qué sistemas tenéis en la empresa?",
                   "¿Con qué herramientas trabajáis ahora mismo?"],
            "en": ["What technologies do you use today?", "Which systems do you have: ERP, CRM, databases?"],
        },
        "datos": {
            "es": ["SAP Business One, Salesforce y Microsoft 365.", "Un ERP propio sobre SQL Server.", "Navision y Excel.",
                   "Odoo y una base de datos MySQL.", "Microsoft 365 y algo de Azure.", "Oracle E-Business Suite.",
                   "Un programa de gestión hecho a medida y poco más.", "Google Workspace y HubSpot."],
            "en": ["SAP, Salesforce and Microsoft 365.", "A custom ERP on SQL Server.", "Odoo and MySQL.",
                   "Google Workspace and HubSpot."],
        },
        "prueba": {
            "es": ["Todo Microsoft: Dynamics, Teams y SharePoint.", "Un AS/400 de hace veinte años."],
            "en": ["Dynamics 365 and SharePoint."],
        },
    },
    "restriccion_presupuestaria": {
        "grupo": "condiciones", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("el presupuesto", "the budget"),
        "preguntas": {
            "es": ["¿Tenéis un presupuesto orientativo para el proyecto?", "¿Con qué presupuesto contáis, más o menos?",
                   "¿Hay algún límite de presupuesto?"],
            "en": ["Do you have a rough budget for the project?", "Is there a budget limit?"],
        },
        "datos": {
            "es": ["Unos 100.000 euros.", "Entre 50.000 y 80.000 €.", "No más de 30.000 euros este año.",
                   "Tenemos 200.000 € aprobados.", "Poco, unos 15.000 euros.", "El máximo son 600.000 € en tres años.",
                   "Alrededor de 40.000 € más el mantenimiento.", "No hay presupuesto cerrado, pero sobre 250.000."],
            "en": ["About €100,000.", "Between 50 and 80 thousand euros.", "No more than €30,000.", "Around 200k."],
        },
        "prueba": {
            "es": ["Nos han dado 70.000 euros para todo.", "Menos de 20.000 €."],
            "en": ["We have €60,000 approved."],
        },
        "no_responde_propio": {
            "es": ["Eso prefiero no decirlo todavía.", "Primero dime tú cuánto costaría."],
            "en": ["I'd rather not say yet."],
        },
    },
    "plazo_objetivo": {
        "grupo": "condiciones", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("el plazo", "the timeline"),
        "preguntas": {
            "es": ["¿Para cuándo necesitaríais tenerlo funcionando?", "¿Tenéis algún plazo para el primer MVP?",
                   "¿Qué plazo os habéis marcado?"],
            "en": ["When would you need it up and running?", "Do you have a deadline for a first MVP?"],
        },
        "datos": {
            "es": ["En seis meses.", "Para final de año.", "Un piloto en tres meses y el resto en un año.",
                   "Cuanto antes, pero sin fecha fija.", "Antes del verano.", "En 2027.", "Para el cierre del trimestre.",
                   "No hay prisa, un año está bien."],
            "en": ["Six months.", "By the end of the year.", "A pilot in three months.", "No rush, a year is fine."],
        },
        "prueba": {
            "es": ["Nos gustaría tener algo en marzo.", "Unas 12 semanas."],
            "en": ["Before summer."],
        },
    },
    "regulacion_y_compliance": {
        "grupo": "condiciones", "no_lo_sabe": True, "no_aplica": True,
        "tema": ("lo de la normativa", "the regulations"),
        "preguntas": {
            "es": ["¿Hay alguna normativa que debamos tener en cuenta, como el RGPD o la ISO 27001?",
                   "¿Tenéis requisitos de compliance?", "¿Tratáis datos personales o sensibles que estén regulados?"],
            "en": ["Are there any regulations we should consider, like GDPR or ISO 27001?", "Any compliance requirements?"],
        },
        "datos": {
            "es": ["El RGPD, tenemos datos de clientes.", "ISO 27001, estamos certificados.",
                   "Datos de salud, así que RGPD y la normativa sanitaria.", "Nada especial más allá del RGPD.",
                   "Somos banco: DORA y lo del Banco de España.", "ENS, porque trabajamos con la administración.",
                   "Solo la LOPD.", "PCI DSS por los pagos con tarjeta."],
            "en": ["GDPR, we handle customer data.", "ISO 27001.", "Nothing beyond GDPR.", "PCI DSS for card payments."],
        },
        "prueba": {
            "es": ["Tenemos datos de pacientes.", "Lo normal, protección de datos."],
            "en": ["Health data, so GDPR plus sector rules."],
        },
        "no_aplica_propio": {
            "es": ["No aplica, no tratamos datos personales.", "No nos afecta ninguna normativa."],
            "en": ["Not applicable, no personal data involved."],
        },
    },
    "kpis_de_exito": {
        "grupo": "proyecto", "no_lo_sabe": True, "no_aplica": False,
        "tema": ("cómo mediréis el éxito", "how you'll measure success"),
        "preguntas": {
            "es": ["¿Cómo vais a medir si el proyecto ha salido bien?", "¿Qué KPIs usaríais para medir el éxito?",
                   "¿Qué tendría que mejorar para que lo consideréis un éxito?"],
            "en": ["How will you measure success?", "Which KPIs would tell you the project worked?"],
        },
        "datos": {
            "es": ["Reducir las roturas de stock un 20 %.", "Que los informes salgan en un día y no en una semana.",
                   "Bajar el coste por envío.", "Que el 80 % de los comerciales use el cuadro de mando.",
                   "Menos devoluciones.", "Ahorrar 10 horas a la semana de trabajo manual.",
                   "Mejorar la precisión de la previsión.", "Que las averías bajen a la mitad."],
            "en": ["Cut stockouts by 20%.", "Reports in one day instead of a week.", "Lower cost per delivery.",
                   "Save ten hours a week."],
        },
        "prueba": {
            "es": ["Que dirección deje de pedir informes por correo.", "Vender más a los clientes actuales."],
            "en": ["Fewer returns."],
        },
    },
}

# --------------------------------------------------------------------------------------------------------
# Respuestas que valen para cualquier campo. {tema} se sustituye por el tema del campo.
# --------------------------------------------------------------------------------------------------------
NO_LO_SABE = {
    "es": ["No lo sé.", "Ni idea, la verdad.", "Pues no lo sé.", "Eso no lo sé.", "Lo desconozco.", "No tengo ni idea.",
           "No sabría decirte.", "Ahora mismo no lo sé.", "Uf, no lo sé, eso lo lleva otro departamento.",
           "No estoy seguro, la verdad.", "No lo tengo claro.", "Desconozco {tema}.", "No sé {tema}, sinceramente.",
           "Eso tendría que preguntarlo, yo no lo sé.", "Sinceramente, no lo sé.", "Aún no lo sabemos.",
           "Ni idea de {tema}.", "No sé.", "nose", "ni idea", "No lo se", "No tengo esa información.",
           "Eso no lo controlo yo, no lo sé.", "No sabría decirte {tema}."],
    "en": ["I don't know.", "No idea.", "Not sure, to be honest.", "I'm not sure.", "I have no idea about {tema}.",
           "Honestly, I don't know.", "I couldn't tell you.", "We don't know yet.", "No clue.",
           "I'd have to ask, I don't know."],
}
NO_LO_SABE_PRUEBA = {
    "es": ["Ni puñetera idea.", "Eso se me escapa, no lo sé.", "Buena pregunta… no lo sé.", "No te lo sé decir.",
           "Pues mira, eso no lo sabemos ni nosotros.", "Lo ignoro."],
    "en": ["Beats me.", "I really don't know, sorry.", "That I don't know."],
}
NO_APLICA = {
    "es": ["No aplica.", "Eso no aplica a nuestro proyecto.", "No nos aplica.", "Eso no es relevante para este proyecto.",
           "No aplica en nuestro caso.", "No lo necesitamos para esto.", "Para este proyecto no aplica.",
           "Lo de {tema} no aplica aquí.", "Eso no entra en este proyecto.", "En nuestro caso no procede.",
           "No hace falta, no aplica."],
    "en": ["Not applicable.", "That doesn't apply to us.", "Doesn't apply to this project.",
           "Not relevant for this project.", "We don't need that for this project."],
}
NO_APLICA_PRUEBA = {
    "es": ["Eso no tiene que ver con lo que queremos hacer.", "No va con este proyecto.", "No procede."],
    "en": ["Not relevant here.", "That's out of scope for us."],
}
NO_RESPONDE = {
    "es": ["¿Y eso para qué lo necesitáis?", "Antes de eso, ¿cuánto costaría más o menos?", "Prefiero verlo más adelante.",
           "Luego te lo paso.", "Déjame que lo mire y te digo.", "¿Podemos pasar a otra cosa?",
           "Eso lo hablamos en otro momento.", "Vale.", "Ok", "¿Qué opciones hay?", "¿Tú qué recomendarías?",
           "Perdona, ¿me lo puedes explicar mejor?", "Ahora no tengo tiempo para eso.",
           "¿Cuánto tardaríais en tenerlo?", "Ya te lo diré.", "Eso te lo confirmo la semana que viene."],
    "en": ["Why do you need that?", "Before that, how much would it cost?", "Let's come back to that later.",
           "I'll get back to you on that.", "Can we move on?", "What would you recommend?",
           "Sorry, can you explain what you mean?"],
}
NO_RESPONDE_PRUEBA = {
    "es": ["Espera, ¿esto lo vais a hacer en remoto?", "Mañana te lo cuento con más calma.", "¿Qué me aconsejas tú?",
           "Hmm, sigamos con otra cosa."],
    "en": ["Can I tell you later?", "What do other clients usually do?"],
}
# Fallo 68: respuestas vagas. Hablan del dato pero no dan nada con que estimar: ni cifra, ni rango, ni nombre.
# Ojo: «Poca cosa, unos cuantos gigas» o «Lo normal, protección de datos» SÍ son datos (llevan una magnitud o un
# nombre); por eso VAGO_ANTES pone a veces una de estas muletillas delante de un dato, que sigue siendo «dato».
VAGA = {
    "es": ["Bastante.", "Depende.", "Lo normal.", "Lo típico.", "Un poco de todo.", "Lo de siempre.", "Algo hay.",
           "Más o menos lo habitual.", "Como cualquier empresa.", "De todo un poco.", "Pues lo normal, ¿no?",
           "Lo estándar.", "Normalito.", "Ni mucho ni poco.", "Lo que tiene cualquiera.", "Pues depende un poco.",
           "Lo habitual en {tema}.", "Lo normal en {tema}, nada raro.", "Pues lo típico, ya sabes."],
    "en": ["Quite a lot.", "It depends.", "The usual.", "A bit of everything.", "Some.", "Like any company.",
           "The standard stuff.", "Pretty normal, I'd say.", "The usual for {tema}."],
}
VAGA_PRUEBA = {
    "es": ["Pues regular, como todos.", "Bueno, lo que viene siendo lo normal.", "Tirando a bastante.", "Uf, de todo.",
           "Lo corriente."],
    "en": ["Fairly standard, I guess.", "A fair amount.", "Nothing out of the ordinary."],
}
VAGAS_PROPIAS = {  # las de cada campo (solo entrenamiento)
    "tamaño_empresa": {"es": ["Bastante gente.", "Somos unos cuantos."], "en": ["Quite a few people."]},
    "tiene_equipo_datos": {"es": ["Algo hay, sí.", "Hay gente que toca datos.", "Más o menos."], "en": ["Sort of."]},
    "tipo_de_servicio": {"es": ["Lo que haga falta.", "Lo que se suele hacer."], "en": ["Whatever's needed."]},
    "objetivo_del_proyecto": {"es": ["Mejorar.", "Ser más eficientes, lo típico.", "Sacar más partido a los datos."],
                              "en": ["Just to improve things."]},
    "modelo_de_despliegue": {"es": ["Lo que se lleve ahora.", "Algo moderno."], "en": ["Whatever's modern."]},
    "cloud_provider": {"es": ["Alguna tenemos.", "La de siempre.", "Una de las grandes."], "en": ["One of the big ones."]},
    "tipo_de_arquitectura": {"es": ["Algo moderno.", "Algo que funcione.", "Lo que se usa ahora."],
                             "en": ["Something that works."]},
    "herramienta_de_visualizacion": {"es": ["La de siempre.", "Una de esas de gráficos.", "Alguna tenemos por ahí."],
                                     "en": ["One of those charting tools."]},
    "enfoque_ai": {"es": ["Lo que se lleva ahora.", "Algo de IA, lo típico.", "Que sea inteligente."],
                   "en": ["Some AI, the usual."]},
    "integration_scope": {"es": ["Varios sistemas.", "Unos cuantos.", "Lo que tenemos."], "en": ["A few systems."]},
    "volumen_de_datos": {"es": ["Bastantes datos.", "Muchos.", "Un montón."], "en": ["Lots of data.", "Quite a bit."]},
    "madurez_analitica_actual": {"es": ["Normalito.", "Regular.", "Como todo el mundo."], "en": ["Average, I guess."]},
    "tamano_equipo_tecnico": {"es": ["Algunos.", "Gente hay.", "Los de siempre."], "en": ["Some people."]},
    "stack_tecnologico_actual": {"es": ["Lo típico.", "Varios programas.", "Lo normal de una empresa."],
                                 "en": ["The usual stuff."]},
    "restriccion_presupuestaria": {"es": ["Lo razonable.", "No mucho.", "Ajustado."],
                                   "en": ["Something reasonable.", "Not much."]},
    "plazo_objetivo": {"es": ["Pronto.", "En breve.", "Más bien pronto."], "en": ["Soon.", "Fairly soon."]},
    "regulacion_y_compliance": {"es": ["Lo de siempre.", "Las normales.", "Lo que toque."], "en": ["The usual ones."]},
    "kpis_de_exito": {"es": ["Que funcione bien.", "Que se note.", "Mejorar en general."], "en": ["That it works."]},
}
VAGAS_PROPIAS_PRUEBA = {
    "volumen_de_datos": {"es": ["Una barbaridad de datos."], "en": ["Tons."]},
    "restriccion_presupuestaria": {"es": ["Lo que veamos sensato."], "en": []},
    "plazo_objetivo": {"es": ["Para dentro de poco."], "en": []},
    "tamano_equipo_tecnico": {"es": ["Algo de gente tenemos."], "en": []},
}
VAGO_ANTES = {  # muletilla vaga delante de un dato: sigue siendo un dato
    "es": ["Lo normal: ", "Pues lo típico, ", "Bastante: ", "Un poco de todo: ", "Lo de siempre, "],
    "en": ["The usual: ", "Quite a lot, "],
}


# Lo que se pone delante de la pregunta del asistente y de la respuesta del cliente, para variar.
INICIO_PREGUNTA = {
    "es": ["", "", "Gracias. ", "Perfecto, apuntado. ", "Entendido. ", "Muy bien. ", "Genial, gracias por el detalle. ",
           "De acuerdo. "],
    "en": ["", "", "Thanks. ", "Got it. ", "Great, noted. ", "Understood. "],
}
DUDA = {  # una respuesta con dato sigue siendo un dato aunque el cliente dude
    "es": ["Creo que ", "Pues ", "A ver, ", "No estoy seguro del todo, pero ", "Diría que ", "Si no me equivoco, "],
    "en": ["I think ", "Probably ", "Not 100% sure, but ", "If I remember right, "],
}
PROPIOS = {"Azure", "AWS", "Google", "Tableau", "Power", "Looker", "Qlik", "Metabase", "SAP", "Salesforce",
           "Navision", "Odoo", "Microsoft", "Oracle", "ISO", "ENS", "PCI", "RGPD", "LOPD", "IT", "DWH", "GDPR", "I",
           "I'm", "Hybrid", "Cloud", "Lakehouse", "Snowflake", "Dynamics", "Qlik", "Data", "DORA"}


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def _minuscula_inicial(texto: str) -> str:
    primera = texto.split(" ", 1)[0].strip(".,:;")
    if primera in PROPIOS or primera.isupper():
        return texto
    return texto[:1].lower() + texto[1:]


def _escrita_a_mano(rng: random.Random, texto: str) -> str:
    """Como escribe la gente en un chat: a veces todo en minúsculas, sin punto final o sin tildes."""
    if rng.random() < 0.2:
        texto = texto.lower()
    if rng.random() < 0.25:
        texto = texto.rstrip(".")
    if rng.random() < 0.1:
        texto = _sin_tildes(texto)
    return texto


def _con_tema(texto: str, campo: dict, idioma: str) -> str:
    return texto.replace("{tema}", campo["tema"][0 if idioma == "es" else 1])


def _respuestas(nombre: str, etiqueta: str, idioma: str, prueba: bool) -> list[str]:
    """Las respuestas posibles de una etiqueta para un campo, sin las de la otra parte (entrenamiento o prueba)."""
    campo = CAMPOS[nombre]
    if etiqueta == "dato":
        return list(campo["prueba" if prueba else "datos"][idioma])
    if etiqueta == "no_lo_sabe":
        base = (NO_LO_SABE_PRUEBA if prueba else NO_LO_SABE)[idioma]
        propias = [] if prueba else campo.get("no_lo_sabe_propio", {}).get(idioma, [])
        return [_con_tema(r, campo, idioma) for r in base] + propias
    if etiqueta == "no_aplica":
        base = (NO_APLICA_PRUEBA if prueba else NO_APLICA)[idioma]
        propias = [] if prueba else campo.get("no_aplica_propio", {}).get(idioma, [])
        return [_con_tema(r, campo, idioma) for r in base] + propias
    if etiqueta == "vaga":
        base = (VAGA_PRUEBA if prueba else VAGA)[idioma]
        propias = (VAGAS_PROPIAS_PRUEBA if prueba else VAGAS_PROPIAS).get(nombre, {}).get(idioma, [])
        return [_con_tema(r, campo, idioma) for r in base] + propias

    # no_responde: frases que no contestan y, tantas como esas, datos de campos de otro grupo
    base = list((NO_RESPONDE_PRUEBA if prueba else NO_RESPONDE)[idioma])
    base += [] if prueba else campo.get("no_responde_propio", {}).get(idioma, [])
    return base + _datos_de_otros(nombre, idioma, prueba)


def _datos_de_otros(nombre: str, idioma: str, prueba: bool) -> list[str]:
    """Respuestas con dato de campos de otro grupo: para el campo preguntado no son una respuesta."""
    grupo = CAMPOS[nombre]["grupo"]
    return [
        r for datos in CAMPOS.values() if datos["grupo"] != grupo
        for r in datos["prueba" if prueba else "datos"][idioma]
    ]


def _ejemplo(rng: random.Random, nombre: str, etiqueta: str, idioma: str, prueba: bool,
             descripciones: dict) -> dict:
    campo = CAMPOS[nombre]
    pregunta = rng.choice(INICIO_PREGUNTA[idioma]) + rng.choice(campo["preguntas"][idioma])
    respuesta = rng.choice(_respuestas(nombre, etiqueta, idioma, prueba))
    if etiqueta == "dato" and campo["grupo"] != "empresa" and rng.random() < 0.2:
        respuesta = rng.choice(DUDA[idioma]) + _minuscula_inicial(respuesta)
    elif etiqueta == "dato" and campo.get("vaga", True) and rng.random() < 0.1:
        # fallo 68: «Lo normal: Power BI» lleva el dato aunque empiece vago
        respuesta = rng.choice(VAGO_ANTES[idioma]) + _minuscula_inicial(respuesta)    
    elif etiqueta == "dato" and rng.random() < 0.1:
        # un dato con otro dato detrás sigue siendo un dato del campo preguntado
        respuesta += " " + rng.choice(_datos_de_otros(nombre, idioma, prueba))
    elif etiqueta == "no_lo_sabe" and rng.random() < 0.12:
        # «no lo sé, pero…» con un dato de otro campo: para el campo preguntado sigue siendo «no lo sabe»
        otro = rng.choice(_datos_de_otros(nombre, idioma, prueba))
        respuesta = respuesta.rstrip(".") + (", pero " if idioma == "es" else ", but ") + _minuscula_inicial(otro)
    respuesta = _escrita_a_mano(rng, respuesta)
    return {
        "state": texto_estado(nombre, descripciones[nombre], pregunta, respuesta),
        "questions": {"estado": PREGUNTA_ESTADO},
        "gold": {"estado": etiqueta},
        "tarea": "estado",
        "campo": nombre,
        "idioma": idioma,
    }


# Ejemplos por campo y etiqueta (entrenamiento, prueba). El español pesa más: es el idioma de casi todos los clientes.
CUANTOS = {"dato": (40, 4), "no_lo_sabe": (26, 3), "no_aplica": (24, 3), "no_responde": (30, 3), "vaga": (24, 3)}
PARTE_EN_INGLES = 0.25


def _etiquetas(campo: dict) -> list[str]:
    """dato y no_responde valen para todos los campos; no_lo_sabe y no_aplica, solo donde tienen sentido; vaga, en
    todos salvo el nombre y el sector."""
    return [e for e in CUANTOS if e in ("dato", "no_responde") or campo.get(e, e == "vaga")]


def generar(prueba: bool, descripciones: dict, rng: random.Random) -> list[dict]:
    filas, vistos = [], set()
    for nombre, campo in CAMPOS.items():
        for etiqueta in _etiquetas(campo):
            objetivo = CUANTOS[etiqueta][1 if prueba else 0]
            intentos = 0
            hechos = 0
            while hechos < objetivo and intentos < objetivo * 30:
                intentos += 1
                idioma = "en" if rng.random() < PARTE_EN_INGLES else "es"
                fila = _ejemplo(rng, nombre, etiqueta, idioma, prueba, descripciones)
                if fila["state"] in vistos:
                    continue
                vistos.add(fila["state"])
                filas.append(fila)
                hechos += 1
    rng.shuffle(filas)
    return filas


def main() -> None:
    plantilla = json.loads((RAIZ / "data_structures" / "problem_case_recovery.json").read_text(encoding="utf-8"))
    descripciones = {nombre: (plantilla[nombre].get("description") or "") for nombre in CAMPOS}
    faltan = sorted(set(CAMPOS) ^ {k for k, v in plantilla.items() if isinstance(v, dict) and k != "evidencias"})
    if faltan:
        raise SystemExit(f"Los campos no coinciden con problem_case_recovery.json: {faltan}")

    rng = random.Random(SEMILLA)
    entrenamiento = generar(False, descripciones, rng)
    prueba = generar(True, descripciones, rng)
    solapadas = {f["state"] for f in entrenamiento} & {f["state"] for f in prueba}
    if solapadas:
        raise SystemExit(f"{len(solapadas)} ejemplos de prueba están también en el entrenamiento")

    SALIDA.mkdir(parents=True, exist_ok=True)
    for nombre, filas in (("entrenamiento", entrenamiento), ("prueba", prueba)):
        with open(SALIDA / f"{nombre}.jsonl", "w", encoding="utf-8") as f:
            for fila in filas:
                f.write(json.dumps(fila, ensure_ascii=False) + "\n")
        idiomas = Counter(fila["idioma"] for fila in filas)
        print(f"{nombre}: {len(filas)} ejemplos (es {idiomas['es']}, en {idiomas['en']})")
        for tarea in sorted({fila["tarea"] for fila in filas}):
            # una fila puede llevar varias preguntas (uso_1, uso_2…): se cuentan las respuestas, no las filas
            reparto = Counter(v for fila in filas if fila["tarea"] == tarea for v in fila["gold"].values())
            print(f"  {tarea}: " + ", ".join(f"{k} {v}" for k, v in sorted(reparto.items())))


if __name__ == "__main__":
    main()
