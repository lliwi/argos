---
name: data-analysis
description: Analizar ficheros de datos (CSV, JSON, Excel) con pandas en el sandbox y producir un resultado verificable.
version: 1.0.0
---
1. Inspecciona antes de calcular: `head`, `wc -l`, y en pandas `df.shape`, `df.dtypes`, `df.head()`.
2. Instala solo lo necesario (`pip install -q pandas`; `openpyxl` si es Excel).
3. Escribe el análisis como script en `out/analisis.py` y ejecútalo con `python out/analisis.py`;
   así el resultado es reproducible y auditable.
4. Guarda los resultados en `out/` (CSV/JSON para datos, `.md` para el informe) y resume en la
   respuesta final las cifras clave con sus unidades.
5. Si hay valores nulos o tipos inesperados, dilo explícitamente en lugar de ocultarlo.
