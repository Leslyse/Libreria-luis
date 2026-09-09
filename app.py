"""
Librería de Luis — completar fichas de libros desde el ISBN.

Lee la tabla de Notion, busca cada ISBN en Google Books y Open Library,
y rellena SOLO las celdas vacías. Nunca pisa lo que ha escrito Luis.

Configuración: todo va en los "Secrets" de Streamlit, nunca en este fichero.
"""

import re
import time
import hmac
import threading
import requests
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed

st.set_page_config(page_title="Librería de Luis", page_icon="📚", layout="centered")

# ----------------------------------------------------------------------------
# Configuración (desde los Secrets de Streamlit)
# ----------------------------------------------------------------------------
TOKEN = st.secrets.get("NOTION_TOKEN", "")
DB_ID = st.secrets.get("NOTION_DB_ID", "")
DS_ID = st.secrets.get("NOTION_DATA_SOURCE_ID", "")
GOOGLE_KEY = st.secrets.get("GOOGLE_BOOKS_KEY", "")
# Google Books geolocaliza al que llama. Desde un servidor no puede, y contesta
# 403 "unknownLocation" salvo que le digamos el país a mano.
GOOGLE_PAIS = st.secrets.get("GOOGLE_BOOKS_COUNTRY", "ES")
PASSWORD = st.secrets.get("APP_PASSWORD", "")

# Versiones de la API de Notion a probar, de más nueva a más antigua.
VERSIONES = ["2026-03-11", "2025-09-03", "2022-06-28"]

# Columna de Notion  ->  clave interna del dato bibliográfico.
#
# Editorial NO está aquí a propósito. Esa columna es un `select` y es el
# desplegable que usa Luis: si la app escribiera en ella, crearía una opción
# nueva por cada variante que devuelvan los catálogos («Newton Compton»,
# «NEWTON COMPTON», «Newton Compton Editori»…) y le ensuciaría la lista.
# La editorial detectada va a una columna de texto aparte, para consultarla.
MAPA = {
    "Título": "titulo",
    "Autor": "autor",
    "Editorial detectada": "editorial",
    "Año edición": "anio",
    "Nº páginas": "paginas",
    "Idioma": "idioma",
    "Peso (g)": "peso",
    "Tamaño": "tamano",
}
COL_ESTADO = "Estado enriquecimiento"      # opcional; se usa solo si existe
COL_EDITORIAL = "Editorial"                # la de Luis: la app nunca la toca
COL_EDITORIAL_AUTO = "Editorial detectada"  # opcional; texto libre

IDIOMAS = {
    "es": "Español", "en": "Inglés", "fr": "Francés", "it": "Italiano",
    "ca": "Catalán", "de": "Alemán", "pt": "Portugués", "eu": "Euskera",
    "gl": "Gallego", "nl": "Neerlandés", "la": "Latín",
}


# ----------------------------------------------------------------------------
# Puerta de contraseña
# ----------------------------------------------------------------------------
def autorizado() -> bool:
    if not PASSWORD:
        st.error("Falta APP_PASSWORD en los Secrets. La app está sin proteger, así que no arranca.")
        return False
    if st.session_state.get("ok"):
        return True
    st.title("📚 Librería de Luis")
    clave = st.text_input("Contraseña", type="password")
    if clave:
        if hmac.compare_digest(clave, PASSWORD):
            st.session_state["ok"] = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    return False


# ----------------------------------------------------------------------------
# Notion: descubrimiento de versión y endpoints
# ----------------------------------------------------------------------------
def cabeceras(version: str) -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }


@st.cache_data(ttl=1800, show_spinner=False)
def descubrir():
    """Encuentra qué versión de API y qué forma de endpoint funcionan.

    Notion cambió las bases de datos a 'data sources'. En vez de adivinar,
    probamos y nos quedamos con la combinación que responde.
    """
    intentos = []
    for version in VERSIONES:
        candidatos = []
        if DS_ID:
            candidatos.append(("data_source", f"https://api.notion.com/v1/data_sources/{DS_ID}"))
        if DB_ID:
            candidatos.append(("database", f"https://api.notion.com/v1/databases/{DB_ID}"))
        for forma, url in candidatos:
            try:
                r = requests.get(url, headers=cabeceras(version), timeout=20)
                intentos.append(f"{version} · {forma} · HTTP {r.status_code}")
                if r.status_code == 200:
                    datos = r.json()
                    props = datos.get("properties", {})
                    if props:
                        return {
                            "version": version,
                            "forma": forma,
                            "url_base": url,
                            "propiedades": {k: v.get("type") for k, v in props.items()},
                            "intentos": intentos,
                        }
            except Exception as e:
                intentos.append(f"{version} · {forma} · {type(e).__name__}")
    return {"error": "Ninguna combinación de versión y endpoint respondió.", "intentos": intentos}


def leer_filas(cfg) -> list:
    """Descarga todas las filas de la tabla, paginando."""
    filas, cursor = [], None
    while True:
        cuerpo = {"page_size": 100}
        if cursor:
            cuerpo["start_cursor"] = cursor
        r = requests.post(f"{cfg['url_base']}/query", headers=cabeceras(cfg["version"]),
                          json=cuerpo, timeout=40)
        r.raise_for_status()
        datos = r.json()
        filas.extend(datos.get("results", []))
        if not datos.get("has_more"):
            return filas
        cursor = datos.get("next_cursor")


# ----------------------------------------------------------------------------
# Lectura y escritura de propiedades
# ----------------------------------------------------------------------------
def valor_de(prop) -> str:
    """Devuelve el contenido de una propiedad como texto; '' si está vacía."""
    if not prop:
        return ""
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop.get(t, [])).strip()
    if t == "number":
        v = prop.get("number")
        return "" if v is None else str(v)
    if t == "select":
        s = prop.get("select")
        return s.get("name", "") if s else ""
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in prop.get("multi_select", []))
    if t == "url":
        return prop.get("url") or ""
    if t == "date":
        d = prop.get("date")
        return d.get("start", "") if d else ""
    if t == "unique_id":
        u = prop.get("unique_id") or {}
        return f"{u.get('prefix','')}-{u.get('number','')}"
    return ""


def empaquetar(tipo: str, valor):
    """Convierte un valor a la forma que espera Notion según el tipo de columna."""
    texto = str(valor).strip()
    if not texto:
        return None
    if tipo == "title":
        return {"title": [{"text": {"content": texto[:2000]}}]}
    if tipo == "rich_text":
        return {"rich_text": [{"text": {"content": texto[:2000]}}]}
    if tipo == "number":
        try:
            return {"number": float(re.sub(r"[^\d.,-]", "", texto).replace(",", "."))}
        except ValueError:
            return None
    if tipo == "select":
        # Notion rechaza las comas en los valores de select.
        return {"select": {"name": texto.replace(",", " ")[:100]}}
    if tipo == "multi_select":
        return {"multi_select": [{"name": texto.replace(",", " ")[:100]}]}
    if tipo == "url":
        return {"url": texto}
    return None


def escribir(cfg, page_id: str, props: dict):
    """Escribe las propiedades. Si Notion rechaza el lote, reintenta una a una
    para salvar lo que sí es válido y saber cuál falla."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    r = requests.patch(url, headers=cabeceras(cfg["version"]),
                       json={"properties": props}, timeout=30)
    if r.status_code == 200:
        return sorted(props.keys()), []

    escritas, fallidas = [], []
    for nombre, valor in props.items():
        rr = requests.patch(url, headers=cabeceras(cfg["version"]),
                            json={"properties": {nombre: valor}}, timeout=30)
        if rr.status_code == 200:
            escritas.append(nombre)
        else:
            detalle = ""
            try:
                detalle = rr.json().get("message", "")[:120]
            except Exception:
                pass
            fallidas.append(f"{nombre}: {detalle or rr.status_code}")
        time.sleep(0.34)
    return escritas, fallidas


# ----------------------------------------------------------------------------
# Fuentes bibliográficas
# ----------------------------------------------------------------------------
UA = {"User-Agent": "libreria-luis/7.0 (catalogacion privada)"}


class Ritmo:
    """Espacia las llamadas: como mucho una cada `intervalo` segundos, entre
    todos los hilos. Google limita por minuto, y ocho hilos a la vez lo pasan."""

    def __init__(self, intervalo: float):
        self.intervalo = intervalo
        self._siguiente = 0.0
        self._c = threading.Lock()

    def esperar(self):
        with self._c:
            ahora = time.monotonic()
            espera = max(0.0, self._siguiente - ahora)
            self._siguiente = max(ahora, self._siguiente) + self.intervalo
        if espera:          # dormimos fuera del cerrojo, si no serializaríamos
            time.sleep(espera)


RITMO_GOOGLE = Ritmo(float(st.secrets.get("GOOGLE_BOOKS_INTERVALO", 0.8)))


def pedir(url: str, timeout: int = 10, intentos: int = 2):
    """GET con un reintento. Resume el error: las trazas de red son ilegibles.

    Si el servidor rechaza la conexión no reintentamos: eso no mejora esperando,
    y multiplicar la espera por cada libro convierte un fallo en una eternidad.
    """
    ultimo = None
    for i in range(intentos):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.exceptions.ConnectionError:
            raise RuntimeError("no responde desde el servidor de la app (conexión rechazada)")
        except Exception as e:
            ultimo = e
            if i + 1 < intentos:
                time.sleep(1.0)
    raise RuntimeError(str(ultimo)[:160])


def sensato(datos: dict) -> dict:
    """Filtro anti-basura: descarta valores imposibles antes de escribirlos."""
    d = dict(datos)
    try:
        p = int(float(d.get("paginas", 0) or 0))
        if p < 8 or p > 5000:
            d.pop("paginas", None)
    except (TypeError, ValueError):
        d.pop("paginas", None)
    try:
        a = int(str(d.get("anio", ""))[:4])
        if a < 1450 or a > 2030:
            d.pop("anio", None)
    except (TypeError, ValueError):
        d.pop("anio", None)
    try:
        g = float(d.get("peso", 0) or 0)
        if g < 20 or g > 15000:
            d.pop("peso", None)
    except (TypeError, ValueError):
        d.pop("peso", None)
    # El autor no puede ser igual que la editorial: ese es el error clásico
    # (salió "Taschen" como autor de Les Dîners de Gala).
    aut, edi = d.get("autor", "").strip().lower(), d.get("editorial", "").strip().lower()
    if aut and edi and aut == edi:
        d.pop("autor", None)
    return {k: v for k, v in d.items() if str(v).strip()}


def google_books(isbn: str) -> dict:
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
    if GOOGLE_PAIS:
        url += f"&country={GOOGLE_PAIS}"
    if GOOGLE_KEY:
        url += f"&key={GOOGLE_KEY}"
    # El 429 por minuto es pasajero: esperamos y reintentamos en vez de dar
    # el libro por perdido. Solo si insiste lo damos por fallo de verdad.
    for intento in range(3):
        RITMO_GOOGLE.esperar()
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code == 429 and intento < 2:
            time.sleep(6 * (intento + 1))
            continue
        break

    if r.status_code != 200:
        # El cuerpo trae el motivo exacto. Sin leerlo estaríamos adivinando,
        # y la URL lleva la clave dentro: nunca la metemos en el mensaje.
        motivo, razon = "", ""
        try:
            err = r.json().get("error", {})
            motivo = err.get("message", "")
            detalles = err.get("errors") or []
            if detalles:
                razon = detalles[0].get("reason", "")
        except Exception:
            motivo = r.text[:200]
        raise RuntimeError(" · ".join(x for x in (f"HTTP {r.status_code}", razon, motivo) if x))
    d = r.json()
    if not d.get("totalItems"):
        return {}
    v = d["items"][0].get("volumeInfo", {})
    return sensato({
        "titulo": v.get("title", ""),
        "autor": ", ".join(v.get("authors", [])),
        "editorial": v.get("publisher", ""),
        "anio": str(v.get("publishedDate", ""))[:4],
        "paginas": v.get("pageCount", ""),
        "idioma": IDIOMAS.get(v.get("language", ""), ""),
    })


def open_library(isbn: str) -> dict:
    r = pedir(f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}"
              "&format=json&jscmd=data")
    d = r.json().get(f"ISBN:{isbn}")
    if not d:
        return {}
    peso = ""
    if d.get("weight"):
        m = re.search(r"([\d.]+)\s*(g|kg|pounds|ounces)", str(d["weight"]), re.I)
        if m:
            n, u = float(m.group(1)), m.group(2).lower()
            peso = {"g": n, "kg": n * 1000, "pounds": n * 453.6, "ounces": n * 28.35}[u]
    return sensato({
        "titulo": d.get("title", ""),
        "autor": ", ".join(a.get("name", "") for a in d.get("authors", [])),
        "editorial": ", ".join(p.get("name", "") for p in d.get("publishers", [])),
        "anio": re.sub(r"\D", "", str(d.get("publish_date", "")))[-4:],
        "paginas": d.get("number_of_pages", ""),
        "peso": round(peso) if peso else "",
        "tamano": str(d.get("dimensions", "") or ""),
    })


def open_library_search(isbn: str) -> dict:
    r = pedir(f"https://openlibrary.org/search.json?q=isbn:{isbn}&limit=1")
    docs = r.json().get("docs", [])
    if not docs:
        return {}
    d = docs[0]
    return sensato({
        "titulo": d.get("title", ""),
        "autor": ", ".join(d.get("author_name", [])),
        "editorial": (d.get("publisher") or [""])[0],
        "anio": str(d.get("first_publish_year", "")),
        "paginas": d.get("number_of_pages_median", ""),
        "idioma": IDIOMAS.get((d.get("language") or [""])[0][:2], ""),
    })


FUENTES = [("Google Books", google_books),
           ("Open Library", open_library),
           ("Open Library (búsqueda)", open_library_search)]


# Cortafuegos: si una fuente encadena fallos, está caída. Seguir preguntándole
# una vez por libro solo sirve para que la barra tarde un cuarto de hora.
LIMITE_FALLOS = 5
_cerrojo = threading.Lock()
_fallos_seguidos: dict = {}
_descartadas: set = set()


def reiniciar_cortafuegos():
    with _cerrojo:
        _fallos_seguidos.clear()
        _descartadas.clear()


def buscar(isbn: str):
    """Consulta las fuentes en cascada y combina lo que devuelven.
    La primera que da un campo, gana."""
    combinado, usadas, errores = {}, [], []
    for nombre, fn in FUENTES:
        with _cerrojo:
            if nombre in _descartadas:
                errores.append(f"{nombre}: descartada tras {LIMITE_FALLOS} fallos seguidos")
                continue
        try:
            datos = fn(isbn)
        except Exception as e:
            errores.append(f"{nombre}: {e}")
            with _cerrojo:
                _fallos_seguidos[nombre] = _fallos_seguidos.get(nombre, 0) + 1
                if _fallos_seguidos[nombre] >= LIMITE_FALLOS:
                    _descartadas.add(nombre)
            continue
        with _cerrojo:
            _fallos_seguidos[nombre] = 0
        if datos:
            usadas.append(nombre)
            for k, v in datos.items():
                combinado.setdefault(k, v)
    return combinado, usadas, errores


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------
if not autorizado():
    st.stop()

st.title("📚 Librería de Luis")
st.caption("Rellena las fichas de los libros a partir del ISBN. Solo toca celdas vacías.")

if not TOKEN or not (DB_ID or DS_ID):
    st.error("Faltan NOTION_TOKEN y NOTION_DB_ID (o NOTION_DATA_SOURCE_ID) en los Secrets.")
    st.stop()

cfg = descubrir()
if "error" in cfg:
    st.error(cfg["error"])
    with st.expander("Detalle de los intentos"):
        st.code("\n".join(cfg["intentos"]))
    st.stop()

if not GOOGLE_KEY:
    st.warning(
        "No hay clave de Google Books. Sin ella, Google agrupa tus consultas con las "
        "de todo el mundo y casi siempre responde 429. Añade GOOGLE_BOOKS_KEY en los Secrets."
    )

ensayo = st.checkbox("Modo ensayo — enseña lo que haría, sin escribir nada", value=True)
solo_pendientes = st.checkbox("Saltar los libros marcados como «sin ficha» en intentos anteriores", value=True)

if st.button("🔎 Buscar y rellenar", type="primary", use_container_width=True):
    props_tabla = cfg["propiedades"]
    columnas = {c: t for c, t in props_tabla.items() if c in MAPA}
    if not columnas:
        st.error("No he reconocido ninguna de las columnas esperadas. Revisa los nombres.")
        st.stop()

    with st.spinner("Leyendo la tabla de Notion…"):
        filas = leer_filas(cfg)

    # Seleccionar qué filas tocar: con ISBN y con algún hueco.
    pendientes = []
    for f in filas:
        p = f.get("properties", {})
        isbn = re.sub(r"[^\dXx]", "", valor_de(p.get("ISBN")))
        if len(isbn) not in (10, 13):
            continue
        if solo_pendientes and COL_ESTADO in props_tabla:
            if valor_de(p.get(COL_ESTADO)).lower().startswith("sin ficha"):
                continue
        huecos = [c for c in columnas if not valor_de(p.get(c))]
        if huecos:
            pendientes.append({
                "id": f["id"], "isbn": isbn, "huecos": huecos,
                "titulo": valor_de(p.get("Título")) or "(sin título)",
                # Si Luis ya eligió editorial, la detectada sobra: sería ruido.
                "editorial_puesta": bool(valor_de(p.get(COL_EDITORIAL))),
            })

    st.info(f"{len(filas)} filas en la tabla · {len(pendientes)} con ISBN y celdas por rellenar.")
    if not pendientes:
        st.success("No hay nada pendiente.")
        st.stop()

    reiniciar_cortafuegos()
    barra = st.progress(0.0, text=f"Consultando catálogos… 0 de {len(pendientes)}")
    resultados = [None] * len(pendientes)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futuros = {ex.submit(buscar, f["isbn"]): i for i, f in enumerate(pendientes)}
        hechos = 0
        for fut in as_completed(futuros):
            resultados[futuros[fut]] = fut.result()
            hechos += 1
            barra.progress(hechos / len(pendientes),
                           text=f"Consultando catálogos… {hechos} de {len(pendientes)}")
    barra.progress(1.0, text="Catálogos consultados.")

    resumen, rellenadas, sin_ficha = [], 0, 0
    fallos_fuente, problemas_escritura = {}, []
    barra2 = st.progress(0.0, text="Escribiendo en Notion…")

    for i, (fila, (datos, usadas, errores)) in enumerate(zip(pendientes, resultados)):
        # Agrupamos por fuente: 300 líneas repetidas no informan más que una.
        for e in errores:
            fuente, _, detalle = e.partition(": ")
            d = fallos_fuente.setdefault(fuente, {"n": 0, "ejemplo": detalle})
            d["n"] += 1
        nuevos = {}
        for col in fila["huecos"]:
            if col == COL_EDITORIAL_AUTO and fila["editorial_puesta"]:
                continue
            clave = MAPA[col]
            if clave in datos:
                paquete = empaquetar(columnas[col], datos[clave])
                if paquete:
                    nuevos[col] = paquete

        # Si fallaron TODAS las fuentes, este libro no se ha consultado: no
        # sabemos si tiene ficha. Marcarlo como «sin ficha» lo condenaría a que
        # las siguientes pasadas lo saltaran para siempre por un fallo de red.
        concluyente = len(errores) < len(FUENTES)
        if nuevos:
            rellenadas += 1
        elif concluyente:
            sin_ficha += 1

        if not ensayo:
            if COL_ESTADO in props_tabla and concluyente:
                paquete = empaquetar(props_tabla[COL_ESTADO],
                                     "completo" if nuevos else "sin ficha")
                if paquete:
                    nuevos[COL_ESTADO] = paquete
            if nuevos:
                escritas, fallidas = escribir(cfg, fila["id"], nuevos)
                if fallidas:
                    problemas_escritura.append(f"{fila['titulo']} → {'; '.join(fallidas)}")
            time.sleep(0.34)   # Notion admite unas 3 peticiones por segundo

        resumen.append({
            "Libro": fila["titulo"][:45],
            "ISBN": fila["isbn"],
            "Rellenado": ", ".join(c for c in nuevos if c != COL_ESTADO) or "—",
            "Fuente": ", ".join(usadas) or "—",
        })
        barra2.progress((i + 1) / len(pendientes), text=f"{i + 1} de {len(pendientes)}")

    barra2.empty()
    verbo = "Se rellenarían" if ensayo else "Rellenados"
    no_consultados = len(pendientes) - rellenadas - sin_ficha
    mensaje = f"{verbo} {rellenadas} libros. Sin ficha en ningún catálogo: {sin_ficha}."
    if no_consultados:
        mensaje += f" No se pudieron consultar: {no_consultados}."
    st.success(mensaje)
    if ensayo:
        st.info("Esto ha sido un ensayo: no se ha escrito nada. Desmarca la casilla para aplicarlo.")
    st.dataframe(resumen, use_container_width=True, hide_index=True)

    if fallos_fuente:
        st.subheader("Fuentes que han fallado")
        for fuente, d in sorted(fallos_fuente.items(), key=lambda x: -x[1]["n"]):
            st.markdown(f"**{fuente}** — {d['n']} consultas fallidas")
            st.code(d["ejemplo"][:400])
        if len(fallos_fuente) == len(FUENTES):
            st.error(
                "Han fallado **todas** las fuentes, así que este resultado no dice "
                "nada sobre la cobertura: no se ha llegado a consultar ningún catálogo. "
                "Arregla los errores de arriba y vuelve a lanzarlo."
            )

    if problemas_escritura:
        with st.expander(f"Filas que Notion rechazó ({len(problemas_escritura)})"):
            st.code("\n".join(problemas_escritura))

with st.expander("Diagnóstico"):
    st.write(f"**Versión de API que funciona:** `{cfg['version']}` · endpoint `{cfg['forma']}`")
    st.write(f"**Clave de Google Books:** {'sí' if GOOGLE_KEY else 'NO'} · país declarado: "
             f"`{GOOGLE_PAIS or 'ninguno'}` · una consulta cada `{RITMO_GOOGLE.intervalo}` s")
    st.write("**Columnas detectadas y su tipo:**")
    st.json(cfg["propiedades"])
