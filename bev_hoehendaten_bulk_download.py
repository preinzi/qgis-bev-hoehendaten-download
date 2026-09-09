"""
BEV Höhendaten Bulk-Download (bundesweit) - QGIS Processing Tool
==================================================================
Lädt für ein gewähltes Gebiet Höhenraster (DGM/DOM, 1m) aus dem bundesweiten
BEV-Datenkatalog (data.bev.gv.at) - deckt ganz Österreich ab, nicht nur ein
einzelnes Bundesland.

Die Kacheln dieses Dienstes sind 50x50 km groß und liegen als Cloud-
optimierte GeoTIFF (COG) vor - potenziell mehrere GB pro Kachel. Statt die
komplette Kachel herunterzuladen, liest dieses Tool per HTTP-Range-Requests
nur den tatsächlich benötigten Ausschnitt direkt aus der Remote-Datei
(GDAL /vsicurl/). Nur falls das nicht funktioniert, wird als Rückfallebene
die komplette Kachel heruntergeladen (gecached) und lokal zugeschnitten.

Sofern nicht explizit ein gewünschtes Koordinatenbezugssystem eingestellt
wird, bleibt alles in der tatsächlichen Original-CRS der Kacheln - das wird
aus einem echten heruntergeladenen Kachelstück gelesen, nicht blind
angenommen (erwartungsgemäß EPSG:3035 / ETRS89 / LAEA Europe, die native
CRS dieses Dienstes).
"""

import math
import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from urllib.request import Request, urlopen

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProject,
    QgsRasterLayer,
)
from osgeo import gdal, osr

# GDAL >= 3.7 warnt, wenn weder UseExceptions() noch DontUseExceptions()
# explizit aufgerufen wurde - ab GDAL 4.0 werden Exceptions Standard sein.
# Explizit aktivieren (wie in QGIS' eigenen gebuendelten GDAL-Algorithmen) -
# das macht das Skript robust gegen Fehler in gdal.Open/Translate/Warp/
# BuildVRT unabhaengig davon, ob die aufgerufene GDAL-Version None oder
# eine Exception zurueckliefert.
gdal.UseExceptions()
osr.UseExceptions()

# Bestaetigt via BEV-Datenkatalog (data.bev.gv.at/geonetwork), Live-Recherche
# 2026. Falls sich die URL-Struktur mal aendert: Produktseite zeigt die
# aktuelle Struktur -> https://www.bev.gv.at/Services/Downloads/Produktbezogene-Downloads/Unentgeltliche-Produkte/DGM.html
BASE_URL = "https://data.bev.gv.at/download/ALS"
BEV_CRS = "EPSG:3035"
TILE_SIZE = 50000  # Meter, Kachelraster in EPSG:3035

# Anzeigename -> (Ordnername im Download-Pfad, Datei-Praefix)
MODEL_TYPES = {
    "DGM": "DTM",
    "DOM": "DSM",
}

# Bekannte Namensvarianten fuer EPSG:3035 als Rueckfallebene, falls
# osr.AutoIdentifyEPSG() eine Kachel-CRS nicht erkennt (bei KAGIS beobachtet:
# schon winzige Gleitkomma-Abweichungen im Ellipsoid reichen, damit der
# exakte Datenbankabgleich scheitert, obwohl der CRS-NAME eindeutig ist).
KNOWN_BEV_CRS_NAMES = {
    "ETRS89-extended / LAEA Europe": "3035",
    "ETRS89 / LAEA Europe": "3035",
}


def tile_url(model_folder, stichtag, n, e):
    fname = f"ALS_{model_folder}_CRS3035RES50000mN{n}E{e}.tif"
    return f"{BASE_URL}/{model_folder}/{stichtag}/{fname}"


def candidate_stichtage(lookback_years=8):
    """BEV veroeffentlicht einmal jaehrlich ein neues Gesamtmosaik, jeweils
    mit Stichtag 15.09. des Vorjahres, verfuegbar ab Anfang des Folgejahres.
    Nicht jede Kachel wird bei jeder Ausgabe aktualisiert - deshalb wird pro
    Kachel rueckwaerts vom aktuellsten bekannten Jahr aus gesucht, bis eine
    tatsaechlich existierende Datei gefunden wird."""
    this_year = date.today().year
    return [f"{y}0915" for y in range(this_year, this_year - lookback_years, -1)]


def url_exists(url, timeout=15):
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "bev-hoehendaten-tool/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True
    except Exception:
        pass
    # manche Server unterstuetzen HEAD nicht sauber - kleinen Range-GET probieren
    try:
        req2 = Request(url, headers={"User-Agent": "bev-hoehendaten-tool/1.0", "Range": "bytes=0-0"})
        with urlopen(req2, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except Exception:
        return False


def find_tile_url(model_folder, n, e):
    for stichtag in candidate_stichtage():
        url = tile_url(model_folder, stichtag, n, e)
        if url_exists(url):
            return url, stichtag
    return None, None


def tiles_for_bbox(bbox3035, tile_size=TILE_SIZE):
    """Liefert alle (n, e) Kachel-Ursprungskoordinaten, die eine bbox
    (xmin, ymin, xmax, ymax) in EPSG:3035 ueberschneiden - reine Arithmetik,
    keine Katalog-Abfrage noetig, da die Kachelnamen die Gitterkoordinaten
    direkt kodieren."""
    xmin, ymin, xmax, ymax = bbox3035
    e0 = math.floor(xmin / tile_size) * tile_size
    e1 = math.floor(xmax / tile_size) * tile_size
    n0 = math.floor(ymin / tile_size) * tile_size
    n1 = math.floor(ymax / tile_size) * tile_size
    tiles = []
    e = e0
    while e <= e1:
        n = n0
        while n <= n1:
            tiles.append((n, e))
            n += tile_size
        e += tile_size
    return tiles


def _gdal_cancel_callback(feedback):
    """GDAL bricht eine laufende Operation (Translate/Warp) sofort ab, wenn
    der Progress-Callback 0 zurueckgibt - das macht 'echtes' Abbrechen
    mitten in einem Fenster-Read moeglich, nicht nur zwischen Kacheln."""
    def _cb(complete, message, user_data):
        return 0 if (feedback is not None and feedback.isCanceled()) else 1
    return _cb


def download_full_tile(url, out_path, feedback=None):
    """Atomarer, gestreamter Volldownload (Chunks statt alles auf einmal im
    Speicher - Kacheln koennen mehrere GB gross sein) mit einem Retry.
    Prueft bei jedem Chunk auf Abbruch, statt erst nach dem ganzen Download.
    Nur die Rueckfallebene, falls der direkte Fenster-Read fehlschlaegt."""
    tmp_path = out_path + ".part"
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "bev-hoehendaten-tool/1.0"})
            with urlopen(req, timeout=600) as resp, open(tmp_path, "wb") as f:
                while True:
                    if feedback is not None and feedback.isCanceled():
                        raise RuntimeError("abgebrochen")
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp_path, out_path)
            return True
        except Exception:
            if feedback is not None and feedback.isCanceled():
                break  # kein Retry mehr versuchen, wenn der Nutzer abgebrochen hat
            continue
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass
    return False


def process_tile(n, e, model_folder, aoi_bbox, out_dir, cache_dir, feedback=None):
    """Liefert den Pfad zu einer bereits auf die AOI zugeschnittenen GeoTIFF
    fuer diese Kachel (oder None). Versucht zuerst den direkten Fenster-Read
    per HTTP-Range aus der Cloud-optimierten Kachel, faellt nur bei Bedarf auf
    einen (gecachten) Volldownload + lokalen Zuschnitt zurueck."""
    tile_bounds = (e, n, e + TILE_SIZE, n + TILE_SIZE)
    window = (
        max(aoi_bbox[0], tile_bounds[0]),
        max(aoi_bbox[1], tile_bounds[1]),
        min(aoi_bbox[2], tile_bounds[2]),
        min(aoi_bbox[3], tile_bounds[3]),
    )
    if window[0] >= window[2] or window[1] >= window[3]:
        return None  # keine echte Ueberschneidung (Rundungsrand)

    url, stichtag = find_tile_url(model_folder, n, e)
    if not url:
        if feedback is not None:
            feedback.pushWarning(
                f"N{n}E{e}: keine {model_folder}-Datei fuer die letzten "
                f"{len(candidate_stichtage())} Jahre gefunden - Kachel uebersprungen.")
        return None

    tile_id = f"N{n}E{e}"
    # Der Dateiname muss das tatsaechlich angeforderte Fenster mit einschliessen,
    # nicht nur Kachel-ID und Stichtag - sonst wuerde eine zweite Anfrage mit
    # einer ANDEREN AOI innerhalb derselben Kachel faelschlich die alte,
    # falsch zugeschnittene Datei wiederverwenden.
    window_key = "_".join(str(int(round(v))) for v in window)
    out_path = os.path.join(out_dir, f"{model_folder}_{tile_id}_{stichtag}_{window_key}.tif")
    if os.path.exists(out_path):
        return out_path

    proj_win = [window[0], window[3], window[2], window[1]]  # ulx, uly, lrx, lry
    tmp_out = out_path + ".part"

    # Primaer: direktes Fenster-Lesen per HTTP-Range aus der Remote-COG.
    try:
        vsicurl_path = "/vsicurl/" + url
        translate_options = gdal.TranslateOptions(
            projWin=proj_win, format="GTiff",
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
            callback=_gdal_cancel_callback(feedback))
        ds = gdal.Translate(tmp_out, vsicurl_path, options=translate_options)
        ok = ds is not None
        ds = None
        if ok and os.path.exists(tmp_out):
            os.replace(tmp_out, out_path)
            return out_path
    except Exception:
        pass
    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    except OSError:
        pass

    if feedback is not None and feedback.isCanceled():
        return None

    # Rueckfallebene: komplette Kachel herunterladen (gecached - ein zweiter
    # Lauf im ueberlappenden Gebiet muss sie nicht nochmal ziehen) und lokal
    # zuschneiden.
    if feedback is not None:
        feedback.pushWarning(
            f"{tile_id}: direktes Fenster-Lesen fehlgeschlagen - lade komplette "
            "Kachel als Rueckfallebene (kann sehr gross sein).")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{model_folder}_{tile_id}_{stichtag}_full.tif")
    if not os.path.exists(cache_path):
        if not download_full_tile(url, cache_path, feedback=feedback):
            if feedback is not None:
                feedback.pushWarning(f"{tile_id}: Volldownload fehlgeschlagen - Kachel uebersprungen.")
            return None
    if feedback is not None and feedback.isCanceled():
        return None
    try:
        translate_options = gdal.TranslateOptions(
            projWin=proj_win, format="GTiff",
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
            callback=_gdal_cancel_callback(feedback))
        ds = gdal.Translate(tmp_out, cache_path, options=translate_options)
        ok = ds is not None
        ds = None
        if ok and os.path.exists(tmp_out):
            os.replace(tmp_out, out_path)
            return out_path
    except Exception:
        pass
    if feedback is not None and feedback.isCanceled():
        return None
    if feedback is not None:
        feedback.pushWarning(f"{tile_id}: Zuschneiden nach Volldownload fehlgeschlagen - Kachel uebersprungen.")
    return None


def run_tile_processing(tiles, model_folder, aoi_bbox, out_dir, cache_dir, feedback,
                         max_workers=3, progress_base=0, progress_span=100):
    """Verarbeitet mehrere Kacheln parallel, abbrechbar innerhalb ca. 1 Sekunde
    (gleiches Muster wie im KAGIS-Tool: kein 'with'-Statement fuer den
    Executor, damit ein Abbruch nicht durch automatisches wait=True beim
    Verlassen des Blocks wieder blockiert)."""
    results = []
    canceled = False
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(process_tile, n, e, model_folder, aoi_bbox, out_dir, cache_dir, feedback): (n, e)
                for n, e in tiles}
        pending = set(futs.keys())
        total = len(pending)
        done_count = 0
        while pending:
            if feedback.isCanceled():
                canceled = True
                feedback.pushInfo(f"Abbruch erkannt - breche {len(pending)} verbleibende Kacheln ab ...")
                for f in pending:
                    f.cancel()
                break
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    r = f.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                done_count += 1
            if total:
                feedback.setProgress(int(progress_base + (done_count / total) * progress_span))
    finally:
        ex.shutdown(wait=not canceled, cancel_futures=canceled)
    return results, canceled


# Grober, aber grosszuegiger Gueltigkeitsbereich fuer Oesterreich in
# EPSG:3035 (aus den 55 BEV-Kacheln abgeleitet: E4250000-E4850000,
# N2550000-N2850000). Dient nur dazu, eine fehlgeschlagene/nicht
# durchgefuehrte CRS-Umrechnung zu erkennen, die sonst still falsche
# (unveraenderte) Koordinaten durchreichen wuerde.
AUSTRIA_3035_SANITY_BOUNDS = (4_000_000, 2_400_000, 5_200_000, 3_000_000)


def looks_like_valid_3035_austria(bbox):
    ax0, ay0, ax1, ay1 = AUSTRIA_3035_SANITY_BOUNDS
    bx0, by0, bx1, by1 = bbox
    return not (bx1 < ax0 or bx0 > ax1 or by1 < ay0 or by0 > ay1)


def check_transform_accuracy(src_authid, dst_authid):
    """Best-effort: prueft ueber pyproj (falls installiert - kein
    Hard-Requirement fuer dieses Tool) die Genauigkeit der besten auf diesem
    System verfuegbaren Umrechnung zwischen zwei CRS. Gibt None zurueck, wenn
    pyproj fehlt oder die Genauigkeit nicht ermittelbar ist - dann wird die
    Umprojektion trotzdem normal durchgefuehrt, nur ohne Vorab-Warnung."""
    try:
        from pyproj.transformer import TransformerGroup
        tg = TransformerGroup(src_authid, dst_authid, always_xy=True)
        if tg.transformers:
            return tg.transformers[0].accuracy
    except Exception:
        return None
    return None


class BevBulkDownload(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    MODEL_TYPES_PARAM = "MODEL_TYPES_PARAM"
    TARGET_CRS = "TARGET_CRS"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, "Gebiet (AOI)"))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODEL_TYPES_PARAM, "Modelltyp(en)", options=list(MODEL_TYPES.keys()),
            allowMultiple=True, defaultValue=[0, 1]))
        self.addParameter(QgsProcessingParameterCrs(
            self.TARGET_CRS, f"Ziel-CRS (leer lassen = {BEV_CRS})", optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, "Zielordner"))

    def processAlgorithm(self, parameters, context, feedback):
        # WICHTIG: parameterAsExtent()/QgsRectangle kann fuer bestimmte
        # Projekt-CRS-Kombinationen beim Parsen des Extent-Strings Werte
        # vertauschen (bei KAGIS reproduzierbar beobachtet und dort behoben -
        # siehe kagis_hoehendaten_bulk_download.py). Vorbeugend genauso: den
        # rohen Parameter-String selbst parsen statt den Objekt-Accessoren
        # zu vertrauen, mit Fallback auf die normale QGIS-Methode falls der
        # Rohwert kein String ist (z.B. bei Aufruf aus dem Modeler).
        raw_value = parameters.get(self.EXTENT)
        match = re.match(
            r"\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)"
            r"\s*(?:\[\s*([^\]]+?)\s*\])?\s*$",
            raw_value) if isinstance(raw_value, str) else None
        if match:
            raw_bbox = tuple(float(match.group(i)) for i in range(1, 5))
            raw_crs = QgsCoordinateReferenceSystem(match.group(5)) if match.group(5) \
                else self.parameterAsExtentCrs(parameters, self.EXTENT, context)
            if raw_crs.authid() == BEV_CRS:
                aoi_bbox = raw_bbox
            else:
                extent_native = self.parameterAsExtent(
                    parameters, self.EXTENT, context, QgsCoordinateReferenceSystem(BEV_CRS))
                aoi_bbox = (extent_native.xMinimum(), extent_native.yMinimum(),
                            extent_native.xMaximum(), extent_native.yMaximum())
        else:
            extent_native = self.parameterAsExtent(
                parameters, self.EXTENT, context, QgsCoordinateReferenceSystem(BEV_CRS))
            aoi_bbox = (extent_native.xMinimum(), extent_native.yMinimum(),
                        extent_native.xMaximum(), extent_native.yMaximum())
        feedback.pushInfo(
            f"AOI in {BEV_CRS}: xmin={aoi_bbox[0]:.1f}, ymin={aoi_bbox[1]:.1f}, "
            f"xmax={aoi_bbox[2]:.1f}, ymax={aoi_bbox[3]:.1f}")

        if not looks_like_valid_3035_austria(aoi_bbox):
            feedback.reportError(
                f"Die AOI wurde nach {BEV_CRS} umgerechnet, liegt aber weit ausserhalb von "
                "Oesterreich (siehe Koordinaten oben - im Millionenbereich waeren sie fuer "
                "Oesterreich in EPSG:3035 zu erwarten). Das deutet auf eine fehlgeschlagene "
                "CRS-Umrechnung hin, oft weil ein benoetigtes PROJ-Datumsgitter auf diesem "
                "System fehlt und die Umrechnung deshalb unveraendert durchgereicht wurde. "
                "Bitte in QGIS unter Einstellungen > Optionen > CRS-Verwaltung den "
                "Netzwerk-Download von PROJ-Gittern aktivieren, oder in den "
                "Projekteigenschaften > Transformationen die betroffene Umrechnung pruefen.")
            return {}

        out_root = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        model_names = [list(MODEL_TYPES.keys())[i]
                       for i in self.parameterAsEnums(parameters, self.MODEL_TYPES_PARAM, context)]
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)

        if target_crs.isValid():
            acc = check_transform_accuracy(BEV_CRS, target_crs.authid())
            if acc is not None and acc > 1.0:
                feedback.pushWarning(
                    f"Achtung: Die beste auf diesem System verfuegbare Umrechnung "
                    f"{BEV_CRS} -> {target_crs.authid()} hat nur {acc} m Genauigkeit - "
                    "groeber als die 1m-Aufloesung der Hoehendaten. Meist fehlt ein "
                    "PROJ-Datumsgitter (Einstellungen > Optionen > CRS-Verwaltung > "
                    "Netzwerk-Download aktivieren). Die Umprojektion wird trotzdem "
                    "durchgefuehrt - fuer volle Genauigkeit ggf. stattdessen in "
                    f"{BEV_CRS} weiterarbeiten, bis das Gitter installiert ist.")
            elif acc is not None:
                feedback.pushInfo(f"Umrechnungsgenauigkeit {BEV_CRS} -> {target_crs.authid()}: {acc} m.")

        tiles = tiles_for_bbox(aoi_bbox)
        feedback.pushInfo(f"{len(tiles)} Kachel(n) betroffen: " +
                           ", ".join(f"N{n}E{e}" for n, e in tiles))

        results = {}
        total_tasks = max(len(model_names), 1)
        task_index = 0
        outer_canceled = False
        for mname in model_names:
            if feedback.isCanceled():
                outer_canceled = True
                break
            model_folder = MODEL_TYPES[mname]
            out_dir = os.path.join(out_root, mname)
            cache_dir = os.path.join(out_root, "_cache", model_folder)
            os.makedirs(out_dir, exist_ok=True)

            task_index += 1
            progress_base = int((task_index - 1) / total_tasks * 100)
            progress_span = 100 / total_tasks
            pieces, canceled = run_tile_processing(
                tiles, model_folder, aoi_bbox, out_dir, cache_dir, feedback,
                progress_base=progress_base, progress_span=progress_span)

            if canceled:
                feedback.pushWarning(f"{mname}: abgebrochen, unvollstaendig verworfen.")
                outer_canceled = True
                break
            if not pieces:
                feedback.pushWarning(f"{mname}: keine Kacheln erfolgreich verarbeitet.")
                continue

            try:
                vrt_path = os.path.join(out_dir, f"{mname}_mosaic.vrt")

                # WICHTIG: NICHT blind BEV_CRS erzwingen, ohne das zu
                # pruefen - genau diese Annahme hat sich beim KAGIS-Tool
                # als falsch herausgestellt (dort fuer einen der beiden
                # Zyklen). Bei BEV als bundesweitem, einheitlichem Datensatz
                # ist es zwar sehr wahrscheinlich, dass wirklich ueberall
                # EPSG:3035 vorliegt - aber ungeprueft war das bisher nur
                # eine Annahme. Stattdessen die echte CRS aus einem
                # tatsaechlich verarbeiteten Kachelstueck auslesen.
                src_ds = gdal.Open(pieces[0])
                tile_wkt = src_ds.GetProjection() if src_ds is not None else ""
                src_ds = None
                if not tile_wkt:
                    feedback.pushWarning(
                        f"{mname}: konnte keine Projektion aus dem Original-Kachelstueck "
                        f"lesen - verwende ersatzweise {BEV_CRS}.")
                    tile_wkt = BEV_CRS
                else:
                    # Drei Ebenen, JEDE EINZELN in try/except (eine Exception
                    # in Ebene 1 darf die folgenden Ebenen nicht verhindern -
                    # genau dieser Fehler ist beim KAGIS-Tool einmal passiert).
                    epsg_code = None

                    try:
                        tile_srs = osr.SpatialReference()
                        tile_srs.ImportFromWkt(tile_wkt)
                        if tile_srs.AutoIdentifyEPSG() == 0:
                            epsg_code = tile_srs.GetAuthorityCode(None)
                    except Exception:
                        tile_srs = None

                    if not epsg_code and tile_srs is not None:
                        try:
                            crs_name = tile_srs.GetName()
                            epsg_code = KNOWN_BEV_CRS_NAMES.get(crs_name)
                            if epsg_code:
                                feedback.pushInfo(
                                    f"{mname}: CRS anhand des Namens '{crs_name}' als "
                                    f"EPSG:{epsg_code} erkannt (exakter Datenbankabgleich "
                                    "war nicht eindeutig).")
                        except Exception:
                            pass

                    if not epsg_code:
                        epsg_code = BEV_CRS.split(":")[1]
                        feedback.pushInfo(
                            f"{mname}: CRS konnte nicht eindeutig bestimmt werden - "
                            f"verwende die fuer BEV-Daten bekannte {BEV_CRS} als Annahme.")

                    if epsg_code:
                        tile_wkt = f"EPSG:{epsg_code}"
                        feedback.pushInfo(f"{mname}: Original-Kachel-CRS als EPSG:{epsg_code} identifiziert.")

                vrt_options = gdal.BuildVRTOptions(outputSRS=tile_wkt)
                gdal.BuildVRT(vrt_path, pieces, options=vrt_options)

                check_ds = gdal.Open(vrt_path)
                final_wkt = check_ds.GetProjection() if check_ds is not None else ""
                check_ds = None
                if final_wkt:
                    feedback.pushInfo(f"{mname}: VRT-Projektion aus Original-Kachelstueck uebernommen.")
                else:
                    feedback.pushWarning(f"{mname}: VRT hat auch nach outputSRS KEINE Projektion - bitte melden!")

                feedback.pushInfo(
                    f"{mname}: VRT erstellt ({len(pieces)} von {len(tiles)} Kachel(n) "
                    f"erfolgreich verarbeitet, {BEV_CRS}) -> {vrt_path}")
                if len(pieces) < len(tiles):
                    feedback.pushWarning(
                        f"{mname}: nur {len(pieces)} von {len(tiles)} Kacheln im Mosaik - "
                        "siehe Warnungen oben fuer den Grund. Mosaik ist entsprechend lueckenhaft.")

                final_path = vrt_path
                if target_crs.isValid():
                    safe_authid = target_crs.authid().replace(":", "_") or "custom_crs"
                    warped_path = os.path.join(out_dir, f"{mname}_mosaic_{safe_authid}.vrt")
                    gdal.Warp(warped_path, vrt_path, dstSRS=target_crs.toWkt(),
                              format="VRT", resampleAlg="cubic")
                    feedback.pushInfo(f"{mname}: nach {target_crs.authid()} umprojiziert -> {warped_path}")
                    final_path = warped_path

                results[mname] = final_path
            except Exception as e:
                feedback.pushWarning(f"{mname}: VRT-Erstellung/Umprojektion fehlgeschlagen ({e}) - uebersprungen.")
                continue

        if outer_canceled:
            feedback.pushInfo(
                f"Abgebrochen - {len(results)} bereits vollstaendig verarbeitete Layer werden trotzdem hinzugefuegt.")

        for label, vrt_path in results.items():
            layer = QgsRasterLayer(vrt_path, label)
            if layer.isValid():
                if not layer.crs().isValid():
                    layer.setCrs(QgsCoordinateReferenceSystem(BEV_CRS))
                    feedback.pushInfo(f"Layer '{label}': CRS manuell auf {BEV_CRS} gesetzt.")
                else:
                    feedback.pushInfo(f"Layer '{label}': CRS automatisch erkannt ({layer.crs().authid()}).")
                QgsProject.instance().addMapLayer(layer)
                feedback.pushInfo(f"Layer '{label}' zum Projekt hinzugefuegt.")
            else:
                feedback.pushWarning(f"Layer '{label}' konnte nicht geladen werden: {vrt_path}")

        summary = f"Fertig: {len(results)} Layer hinzugefuegt."
        if outer_canceled:
            summary += " Lauf wurde vom Nutzer abgebrochen."
        feedback.pushInfo(summary)

        return {}

    def name(self):
        return "bev_hoehendaten_bulk_download"

    def displayName(self):
        return "BEV Höhendaten Bulk-Download (bundesweit)"

    def group(self):
        return "BEV"

    def groupId(self):
        return "bev"

    def shortHelpString(self):
        return (
            "<p>Lädt Höhenraster (DGM/DOM, 1m) für das gewählte Gebiet aus dem "
            "bundesweiten BEV-Datenkatalog - deckt ganz Österreich ab, nicht "
            "nur ein einzelnes Bundesland.</p>"
            "<p>Die Kacheln sind 50×50 km groß und potenziell mehrere GB. "
            "Statt die komplette Kachel herunterzuladen, liest das Tool nur "
            "den tatsächlich benötigten Ausschnitt per HTTP-Range-Requests "
            "direkt aus der Cloud-optimierten GeoTIFF. Nur falls das nicht "
            "funktioniert, wird als Rückfallebene die komplette Kachel "
            "heruntergeladen und lokal zugeschnitten.</p>"
            f"<p>Original-CRS wird direkt aus einer echten Kachel gelesen (erwartungsgemäß "
            f"{BEV_CRS}, ETRS89 / LAEA Europe - die native CRS dieses Dienstes); mit "
            "Ziel-CRS wird zusätzlich ein virtuell umprojiziertes VRT "
            "erzeugt.</p>"
            "<p>Quelle: Bundesamt für Eich- und Vermessungswesen (BEV), "
            "https://www.bev.gv.at, CC-BY-4.0</p>"
        )

    def createInstance(self):
        return BevBulkDownload()
