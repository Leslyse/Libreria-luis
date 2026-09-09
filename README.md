# Librería de Luis — app para completar fichas

App con un botón: lee la tabla de Notion, busca cada ISBN en Google Books y
Open Library, y rellena **solo las celdas vacías**. Nunca pisa lo que ha
escrito Luis.

---

## Puesta en marcha (una sola vez)

### 1. Clave de Google Books

Sin clave, Google mete tus consultas en un cubo compartido con todo el mundo y
responde `429` casi siempre. Con clave, es gratis.

1. Entra en <https://console.cloud.google.com/> con tu cuenta de Google.
2. Crea un proyecto (nombre libre, por ejemplo `libreria-luis`).
3. Ve a **APIs y servicios → Biblioteca**, busca **Books API** y pulsa **Habilitar**.
4. Ve a **APIs y servicios → Credenciales → Crear credenciales → Clave de API**.
5. Copia la clave. Empieza por `AIza...`.

### 2. Repositorio en GitHub

Sube estos tres ficheros (`app.py`, `requirements.txt`, `README.md`) a un
repositorio nuevo. Puede ser privado.

**No subas nunca el token de Notion ni la clave de Google.** Van en el paso 3.

### 3. Desplegar en Streamlit

1. Entra en <https://share.streamlit.io> con tu cuenta de GitHub.
2. **Create app** → elige el repositorio → fichero principal `app.py` → **Deploy**.
3. En **⚙️ Settings → Secrets**, pega esto con tus valores reales:

```toml
NOTION_TOKEN = "ntn_..."
NOTION_DB_ID = "34da45539f2846a2980b0ae237903016"
NOTION_DATA_SOURCE_ID = "c776f76c-41fc-4919-94cc-ff174c61c69e"
GOOGLE_BOOKS_KEY = "AIza..."
APP_PASSWORD = "la que elijas para Luis"
```

Guarda. La app se reinicia sola y ya tienes la URL.

### 4. Conectar la integración de Notion a la tabla

En Notion, abre la base **📕 libros** → menú `···` → **Conexiones** → añade tu
integración. Sin esto, el token no ve la tabla.

### 5. El botón en Notion

En la página **📚 Librería de Luis**, añade un botón:

- Tipo de acción: **Abrir página o URL**
- URL: la de tu app
- Nombre: `🔄 Completar fichas`

Luis pulsa ahí, escribe la contraseña una vez y le sale la pantalla con el botón.

---

## Columna opcional recomendada

Crea en la tabla una columna **`Estado enriquecimiento`** de tipo *Select*.
La app la rellena con `completo` o `sin ficha`, y en las siguientes pasadas
salta los libros que ya se sabe que no están en ningún catálogo. Sin esta
columna la app funciona igual, solo que reintenta lo imposible cada vez.

---

## Uso diario

1. Luis mete ISBNs en Notion.
2. Pulsa el botón.
3. Deja marcado **modo ensayo** para ver qué haría.
4. Si tiene buena pinta, desmarca y vuelve a pulsar.

La app tarda unos segundos: consulta los catálogos de ocho en ocho a la vez.

---

## Si algo falla

Abre el desplegable **Diagnóstico** al final de la página. Dice qué versión de
la API de Notion está funcionando, si la clave de Google está puesta, y el tipo
exacto de cada columna detectada.

Errores típicos:

| Síntoma | Causa casi segura |
|---|---|
| «Ninguna combinación de versión y endpoint respondió» | La integración no está conectada a la tabla (paso 4), o el token está mal |
| Google Books siempre da 429 | Falta `GOOGLE_BOOKS_KEY` |
| Una columna concreta falla en todas las filas | Su tipo en Notion no admite ese valor. El aviso dice cuál es |

---

## Nota sobre la calidad de los datos

**Editorial, Año y Nº de páginas** salen fiables. **Autor** no: alrededor de
uno de cada diez viene mal en las fuentes automáticas. La app descarta el caso
más común —que el autor sea en realidad la editorial— pero conviene revisar los
autores de los libros valiosos a mano.
