# kiln population — WorldPop denominators from the snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kiln population` sums a WorldPop-style population raster under every admin unit at a chosen level of the snapshot, rolls the sums up the `partOf` hierarchy, and writes one `ICRTargetPopulation` Group per unit as FHIR NDJSON that `kiln load` can send — with no GDAL, GEOS or Python.

**Architecture:** Four small pieces. A pure-Rust **GeoTIFF reader** (`tiff` crate over an in-memory buffer) exposes tiles behind a `Raster` trait. A **zonal sum** rasterises each polygon with a scanline / even-odd fill under the *pixel-centroid* rule — a pixel belongs to the polygon its centre falls in — so non-overlapping children sum exactly to their parent (pixel's `all_touched` over-counts every shared boundary). A **fetch** step resolves `--raster` from a local path or an http(s) URL, cached once under `SNAPSHOT/rasters/` through the existing content-addressed `Cache`. The **command** reuses `build_index` (pass one of transform) for records + hierarchy, measures units at `--level`, rolls up, and emits Groups via a new `fhir/group.rs`.

**Tech Stack:** Existing crate (Rust stable, clap 4, serde_json, geo 0.33, reqwest via `FhirClient`, sha2 via `Cache`) plus `tiff = 0.11` with only the `deflate` feature (flate2 → miniz_oxide, pure Rust). Dev: `httptest` for the URL fetch test, one 2.5 KB real COG fixture.

**Repo:** `~/github/kiln`. `cargo` is not on PATH in this shell; prefix every cargo command with
`export PATH="$(dirname "$(rustup which cargo)"):$PATH"` (or run `rustup run stable cargo …`).
Baseline before Task 1: `cargo test` → 231 unit tests + all integration suites pass (verified 2026-09-08).

---

## Why this shape (design summary — there is no separate spec)

- **Where the numbers go.** The ICR IG (`~/github/icr/ig/input/fsh/profiles-population.fsh:55-86`) models population as a `Group` (`ICRTargetPopulation`, `actual=false`, `quantity` = count, `characteristic[geography]` → `Reference(Location)`), never as a Location attribute. The `worldpop` code already exists in `icr-denominator-source-cs` (`codesystems.fsh:191`). kiln therefore emits Groups and leaves Locations untouched. This is kiln's first non-Location/Organization resource.
- **Why kiln and not pixel.** pixel (`~/github/pixel`) is a FastAPI + Temporal + PostGIS service. What the ICR needs from it is 25 lines (`worker/activities.py:194-218`) and the public COGs on Cloudflare R2: `https://pub-e6e33253461449dfb455784209fd31bb.r2.dev/{iso3}_pop_{year}_CN_100m_cog.tif` (WorldPop R2025A constrained, 2026–2030, Float32, nodata `-99999`, DEFLATE, 256 px tiles, EPSG:4326). kiln already has the boundaries, the hierarchy, and the NDJSON → `load` path.
- **Why pixel-centroid, not all-touched.** pixel's admin path uses `rasterio.mask(all_touched=True)`; its own docstring (`worker/analyses/quadkey_enrich.py:12-15`) says this double-counts shared pixels. For a registry denominator the sums must be additive: LGAs → state → country. Centroid assignment partitions pixels exactly. Coverage weighting (exactextract) is also exact but needs polygon∩pixel clipping; not worth it at 100 m for admin units.
- **Why measure one level and roll up.** Parents are sums of children by construction (`is-calculated = true`), and the rollup is auditable: the report flags every parent whose descendants were missing a boundary.
- **Whole file in memory, not range requests.** A 100 m country COG is 100–200 MB compressed (Nigeria 2026: 167 MB). Fetch once, cache, decode tiles from a `Cursor<Vec<u8>>`. Simpler than an HTTP `Read + Seek` adapter and ~15k range requests.
- **Ids are deterministic** (`pop-{source}-{year}-{locationId}`) so a re-run updates the same Groups in place; `kiln load` PUTs by id with no `ifMatch` (a create-or-replace), which is right for a resource kiln fully owns.

**Out of scope (follow-ups in the ICR repo, not here):** swapping `tools/campaign-builder/campaign_builder/population.py` to read these Groups; a `tools/hapi/README.md` recipe; age–sex bands (WorldPop's total-only COGs); H3-style per-cell population.

## Existing APIs you will use

- `src/error.rs`: `KilnError::{Usage(String), Io{path,source}, Json, Environment(String)}`, `KilnError::io(path, e)`, `exit_code()` (Usage → 2, else 1), `type Result<T>`.
- `src/report.rs`: `Report::default()`, `add(kind, location_id, detail)`, `counts()`, `summary()`, `to_json()`; `#[cfg(test)] count(kind)`.
- `src/transform.rs`: `pub fn write_report(path: &Path, report: &Report) -> Result<()>`.
- `src/index/mod.rs` + `build.rs`: `pub fn build_index(ndjson: &Path, country_override: Option<&str>) -> Result<Index>`; `Index { records: Vec<IndexRecord>, hierarchy: Hierarchy, order, report: Report, read }`; `IndexRecord { id, name: Option<String>, part_of, type_code, offset: u64, len: usize, geometry: Option<GeometrySummary>, .. }`.
- `src/index/hierarchy.rs`: `Hierarchy::get(i) -> Option<&HierarchyInfo>`; `HierarchyInfo { admin_level: Option<i8>, parent: Option<NodeIdx>, .. }`; `NodeIdx::index() -> usize`. `admin_level` is `Some` only for admin units (`type_code == "admin-unit"`), `0` for the country.
- `src/fhir/ndjson.rs`: `LineAccess::open(&Path)`, `read_at(offset, len) -> Result<String>`.
- `src/fhir/location.rs`: `Location::parse(&Value, &mut Report) -> Option<Location>`; `Location { id, name, boundary: Option<Boundary>, .. }`; `Boundary::{Inline(Vec<u8>), Url(String)}`.
- `src/geometry/mod.rs`: `pub use geojson::parse_boundary` — `parse_boundary(bytes: &[u8], id: &str, report: &mut Report) -> Option<geo::Geometry<f64>>`.
- `src/extract/cache.rs` (`pub mod`): `Cache::new(&Path)`, `read(url) -> Result<Option<Vec<u8>>, String>`, `write(url, &[u8]) -> Result<(), String>`.
- `src/extract/client.rs` (`pub mod`): `FhirClient::new(token: Option<String>, retries: usize, timeout: Duration) -> Result<FhirClient>`, `get(url) -> Result<Fetched { body: Vec<u8> }, FetchError>` (retries 429/5xx with backoff; `FetchError` implements `Display`).
- `src/snapshot/mod.rs`: `LOCATIONS_FILE`, `Snapshot::new(&Path)` with path helpers.
- `src/cli.rs`: `DEFAULT_RETRIES`, `DEFAULT_TIMEOUT_SECS`; `src/cells.rs` (`kiln index`) is the pattern for a snapshot → NDJSON command.
- `tiff 0.11` (decoder): `Decoder::new(impl Read + Seek)`, `dimensions() -> (u32,u32)`, `get_chunk_type() -> ChunkType::{Tile,Strip}`, `chunk_dimensions() -> (u32,u32)`, `chunk_data_dimensions(idx) -> (u32,u32)` (edge chunks are smaller, no padding), `read_chunk(idx) -> DecodingResult` (`F32(Vec<f32>)` for IEEE-float 32-bit samples, row-major with `data_width` values per row), `find_tag(Tag) -> TiffResult<Option<ifd::Value>>`, `find_tag_unsigned_vec::<u16>(Tag) -> TiffResult<Option<Vec<u16>>>`; `ifd::Value::{into_f64_vec(), into_string()}`; `Tag::{ModelPixelScaleTag, ModelTiepointTag, GdalNodata, GeoKeyDirectoryTag}`. Tile index = `ty * tiles_across + tx`; strip index = `ty`.

## File structure

```
Cargo.toml                       + tiff
src/
  cli.rs                         + PopulationArgs, Command::Population
  main.rs                        + mod population; mod raster; match arm
  snapshot/mod.rs                + RASTERS_DIR, Snapshot::rasters()
  raster/
    mod.rs                       GeoTransform, Tile, Raster trait, MemRaster (test-only)
    geotiff.rs                   GeoTiff: tiff::Decoder over Cursor<Vec<u8>>, implements Raster
    zonal.rs                     scanline even-odd fill, zonal_sum, raster_total
    fetch.rs                     --raster → bytes: local file or URL via Cache + FhirClient
  fhir/
    mod.rs                       + pub mod group
    group.rs                     ICRTargetPopulation Group JSON, constants, group_id
  population.rs                  run_population: index → measure → roll up → NDJSON + report
tests/
  fixtures/raster/pop_small_cog.tif   40×30 Float32 COG, 16 px tiles (Task 1)
  population.rs                  end-to-end through the binary
README.md                        Commands, ### population, report kinds, layout, non-goals
```

## The fixture and its expected numbers (used throughout)

`tests/fixtures/raster/pop_small_cog.tif`: 40 columns × 30 rows, pixel 0.1°, origin lon 3.0 / lat 7.0 (so it covers lon 3–7, lat 4–7), Float32, DEFLATE, no predictor, 16×16 tiles (3 across × 2 down; the right column of tiles is 8 px wide, the bottom row 14 px tall), GDAL_NODATA `-99999`. Pixel value at (row r, col c) is `r*100 + c + 1`, except three nodata pixels at (0,0), (5,5), (29,39).

Pixel-centroid sums (computed independently in Python while writing this plan):

| polygon (lon/lat) | sum | data pixels |
|---|---|---|
| whole raster, lon 3–7 × lat 4–7 (or anything larger) | 1 761 153 | 1197 |
| top-left square, lon 3.0–3.5 × lat 6.5–7.0 | 5 074 | 24 |
| left half, lon 3–5 | 875 793 | 598 |
| right half, lon 5–7 | 885 360 | 599 |
| triangle (3,7) (7,7) (3,4) | 578 783 | 598 |
| mid box, lon 4.5–5.9 × lat 5.2–6.7 (spans 4 tiles) | 214 725 | 210 |
| box far away, lon 20–21 × lat 20–21 | 0 | 0 |
| whole raster with the top-left square as a hole | 1 756 079 | 1173 |

left + right = whole: that additivity is the property the design buys.

---

### Task 1: Dependency, fixture, and the `Raster` trait

**Files:**
- Modify: `Cargo.toml:12-30`
- Create: `tests/fixtures/raster/pop_small_cog.tif`
- Create: `src/raster/mod.rs`
- Modify: `src/main.rs:1-15`

- [ ] **Step 1: Add the tiff dependency**

In `Cargo.toml` `[dependencies]`, after `geo-traits = "0.3"`:

```toml
tiff = { version = "0.11", default-features = false, features = ["deflate"] }
```

Run: `cargo build 2>&1 | tail -3` → expected `Finished` (no native libraries appear; `flate2` defaults to `miniz_oxide`).

- [ ] **Step 2: Add the COG fixture**

The file already sits at `tests/fixtures/raster/pop_small_cog.tif` if this plan was authored on the same machine (it was copied there on 2026-09-08). If it is missing, recreate it either with GDAL:

```bash
cd /tmp && python3 - <<'EOF'
W,H=40,30
nodata={(0,0),(5,5),(29,39)}
with open('src.asc','w') as f:
    f.write(f"ncols {W}\nnrows {H}\nxllcorner 3.0\nyllcorner {7.0-H*0.1}\ncellsize 0.1\nNODATA_value -99999\n")
    for r in range(H):
        f.write(' '.join('-99999' if (r,c) in nodata else str(r*100+c+1) for c in range(W))+'\n')
EOF
gdal_translate -q src.asc pop_small_cog.tif -of COG -ot Float32 -a_srs EPSG:4326 -a_nodata -99999 \
  -co COMPRESS=DEFLATE -co BLOCKSIZE=16 -co OVERVIEWS=NONE   # warns that 16 < 128; it still writes 16 px tiles
mkdir -p ~/github/kiln/tests/fixtures/raster && cp pop_small_cog.tif ~/github/kiln/tests/fixtures/raster/
```

or, without GDAL, decode this base64 (2548 bytes) into the same path:

```
SUkqAMAAAABHREFMX1NUUlVDVFVSQUxfTUVUQURBVEFfU0laRT0wMDAxNDAgYnl0ZXMKTEFZT1VU
PUlGRFNfQkVGT1JFX0RBVEEKQkxPQ0tfT1JERVI9Uk9XX01BSk9SCkJMT0NLX0xFQURFUj1TSVpF
X0FTX1VJTlQ0CkJMT0NLX1RSQUlMRVI9TEFTVF80X0JZVEVTX1JFUEVBVEVECktOT1dOX0lOQ09N
UEFUSUJMRV9FRElUSU9OPU5PCiAAEwAAAQMAAQAAACgAAAABAQMAAQAAAB4AAAACAQMAAQAAACAA
AAADAQMAAQAAAAgAAAAGAQMAAQAAAAEAAAAVAQMAAQAAAAEAAAAcAQMAAQAAAAEAAAA9AQMAAQAA
AAEAAABCAQMAAQAAABAAAABDAQMAAQAAABAAAABEAQQABgAAAFICAABFAQMABgAAAGoCAABTAQMA
AQAAAAMAAAAOgwwAAwAAALIBAACChAwABgAAAMoBAACvhwMAIAAAAPoBAACwhwwAAgAAADoCAACx
hwIACAAAAEoCAACBpAIABwAAAKoBAAAAAAAALTk5OTk5AACamZmZmZm5P5qZmZmZmbk/AAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIQAAAAAAAABxAAAAAAAAAAAABAAEAAAAH
AAAEAAABAAIAAQQAAAEAAQAACAAAAQDmEAEIsYcHAAAABggAAAEAjiMJCLCHAQABAAsIsIcBAAAA
iG10lh2kckAAAABAplRYQVdHUyA4NHwAegIAAAAEAAB6BQAAgQYAAMMHAAAFCQAAfgFyAf8AOgE6
AesAfgEAAHicFdDLbg9xHMbh1x1YOa16B4obGPpfKKXOLMdpb+2U7xZ76pgJ9ljT5JdYoKoaERER
mYiIa3B+msyTdz75zmpq/vmLJJ2HYqAxkp3JeqaYpuMAPecolnYly6ywyjve84GPfOIzXxj5yje+
84PZmWQPe5ljH/uZ5wAHOcRhjnCUYxznxEzqur1hyYIlNy25ZcltS+5YcteSe/alfWXJkiWvLVm2
5I0lK5a8tWTV/rS/LPltf8/U2j+tP/qvJv8sycSfnqRoZJ3eoGlko6ZoZJOmaGSzpmhki96qaWRa
UzSyTVM0sl1TNLJD79Y0MqspGtmjKRrZqykamdMnNY2c0hSNnNYUjZzRFI2c1ec1jVzQFI1c1BSN
XNIUjVyeZLjinZFcnWSKjp5ioDGSa+509NRaX9f0FAONkdxwp6OnGGiMZEHf14zkge/p6CkGGiN5
6E5HT631I01PMdAYyWN3OnqKgcZInuinmpE88z0dPcVAYySL7nT01OLkP9uNF0vbjRdLcgEAAHic
Fco5j0hxFMbhdxZfwhbfwdZeZkpba7+WWXwJ/joJEuVQcDEzUVoaDXNK5diVtxQRmYjIREQ8Uzx5
88s5yY1DyR2WGHjCU15SvGGdL4x8ZYNNcjj5xnd+sMFPfvGbTf7wl39kJplgkimmOckpTnOGs5zj
PD0XuMglLjPHPAsszqTdtw8sGSx5aMkjSx5bsmzJiiWrdt2+teSdJe8t+WDJR0s+WfLZki8z6SZm
0ygyOZuORpEpTaPItKZRZJverimyQ9MoslPTKLJL0yiyW+/VFNmnaRTZr2kUOaBpFDmoj2iKHNU0
ihzTNIoc1zSKnNBzmiLzmkaRBU2jyKKmUeSKvqopck3TKNJms4eOnsZAMZLr+qZmJLf809HTGChG
ctudjp621UuansZAMZK77nT0NAaKkdzTy5qRrPino6cxUIxk1Z2OnrbVzzQ9jYFiJM/d6ehpDBQj
eaFfaUby2j8dPY2BYiRr7nT0tLXZ/7XTElG10xJR/wAAAHicjc27UQMxFIXhE+AHtgMCAgIGtgtD
pmG3AKACpX5gu4RrcrsASDbyAink3DJ4BSqFvwNJM98oOf9c6ehGGmKCE5ziDOeokHu9WupjgCGO
McIYkzrfz9jMscASD1hhjU2+twObrpZBL/zQKz/0VtB/sflmD/3wQ7/80F++D71GBof6jQIMDg2a
fH/BHg5d0sPgUFXQT9nDoSt6GBy6Luhv2cOhO3oYHLov6Gfs4dCcHgaHFvm+2tIgwtDCkaDHfN/u
2CNB+0YVAiJsX3D/iT0iDC0cCXouuH9gjwR13EdAhHUF99/ZI8LQwpGgj4L7n+yRIOc+AiLMs/0/
LAGA4SwBgOE6AQAAeJzFzSGOFjEYx+E3AQOGVYuBZAIGFDWgK771c4QeYY7QI8wReoQ5QuWCGrGg
C2igGhA83ylo8oh/3l/SWO4vkSlUGp1BfLzEQqZQaXQG8cl+sBnEZz2ZQqXRGcQXdzKFet1fbQqV
RmcQ39zJFCqNziC+2z9sBvFTT6ZQaXQG8cudTKFe92+bQqXRGcQfdzKFSqMziL+XWB/dRWGjstM4
6JwMJvH4Lm5YSGT2J3oOOieDSTzVs5DIrBQ2KuczPZO40bOQyKwUNio7jYN+7W/1JDIrhY3KTuOg
czKYxHP9Cz0blZ3GQedkMImX/mMhkdlf6TnonAwm8VrPQiKzUtionG/0TOKtnoVEZqWwUdlpHPRr
/05PIrNS2KjsNA46J4NJJP17PRuVncZB52QwiQ/+YyGR+c/vHwkr+QAJK/kAOgEAAHicxc0hjhYx
GMfhV4ABsygwkEnAsKqYxVZ86+cIPcIcoUeYI/QIc4RqWDEKVjYBNMUD4flOQZNH/PP+ksby6RKZ
QqXRGcTnSyxkCpVGZxAP9hebQXzVkylUGp1BPLqTKdTr/mZTqDQ6g/juTqZQaXQG8cP+aTOIqSdT
qDQ6g/jlTqZQr/u3TaHS6AzijzuZQqXRGcTfS6xP7qOwUdlpHHROBpN4eh83LCQy+zM9B52TwSSe
61lIZFYKG5XzRs8kXuhZSGRWChuVncZBv/Yv9SQyK4WNyk7joHMymMQr/Ws9G5WdxkHnZDCJN/5j
IZHZ3+o56JwMJvFOz0Iis1LYqJzv9UziVs9CIrNS2KjsNA76tU96EpmVwkZlp3HQORlM4oP+Ts9G
Zadx0DkZTOKj/1hIZP7z+wcPWPowD1j6MOsAAAB4nMXPIVLDQBSA4TUoEKWqqhMBhppU1Ua0mhxh
j5Aj7BFyhD1CjpAZBAZmKqmLAR16AOC7wcbxZj73v3nzQvV2DA2RRGZkIrxTmPyhZyJcjqGiIZIu
5f3qU08kkRmZCF8L7n/rmQhX92mIpOuC+z96IonMyET4Le+3N6cQ6Uj0ZAZGStPf6hkYOTMxE+7K
++d7PTNhfQorKmqadXl/tdFT09AS6Uib8n671dOR6MkMjNsF/z/oGRg5MzETHhf8/6RnJuz8T0VN
s1vw/15PTUNLpCPtF/x/0NOR6MkMh1N6fnkt7v/z/AH7L38n+y9/Jw==
```

Verify: `shasum -a 256 tests/fixtures/raster/pop_small_cog.tif` and `ls -l` → 2548 bytes. (`gdalinfo` if available: size 40×30, block 16×16, Float32, nodata -99999, geoTransform `[3.0, 0.1, 0.0, 7.0, 0.0, -0.1]`.)

- [ ] **Step 3: Write the failing test for `GeoTransform`**

Create `src/raster/mod.rs`:

```rust
//! Rasters kiln can read without GDAL: a GeoTIFF decoded by the pure-Rust
//! `tiff` crate, plus the georeferencing that maps a pixel to a WGS84
//! coordinate. Only north-up geographic (lon/lat) rasters — WorldPop's
//! layout — are supported; anything else is a usage error, never a silent
//! misplacement.

pub mod fetch;
pub mod geotiff;
pub mod zonal;

use crate::error::Result;

/// Affine georeferencing for a north-up raster. Pixel (col, row) covers
/// lon [x0 + col·px, x0 + (col+1)·px) and lat (y0 − (row+1)·py, y0 − row·py].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GeoTransform {
    pub x0: f64,
    pub y0: f64,
    /// Pixel width in degrees, > 0.
    pub px: f64,
    /// Pixel height in degrees, > 0 (rows run north to south).
    pub py: f64,
}

impl GeoTransform {
    /// Fractional column of a longitude: 0.0 is the left edge of column 0.
    pub fn col_of(&self, lon: f64) -> f64 {
        (lon - self.x0) / self.px
    }
    /// Fractional row of a latitude: 0.0 is the top edge of row 0.
    pub fn row_of(&self, lat: f64) -> f64 {
        (self.y0 - lat) / self.py
    }
    pub fn lon_center(&self, col: u32) -> f64 {
        self.x0 + (f64::from(col) + 0.5) * self.px
    }
    pub fn lat_center(&self, row: u32) -> f64 {
        self.y0 - (f64::from(row) + 0.5) * self.py
    }
}

/// One decoded chunk. Edge chunks are smaller than `Raster::tile_size`;
/// `data` is row-major with `width` values per row and no padding.
#[derive(Debug, Clone, PartialEq)]
pub struct Tile {
    pub width: u32,
    pub height: u32,
    pub data: Vec<f32>,
}

/// A single-band float raster read one tile at a time.
pub trait Raster {
    fn width(&self) -> u32;
    fn height(&self) -> u32;
    fn transform(&self) -> &GeoTransform;
    fn nodata(&self) -> Option<f32>;
    /// Nominal (tile_width, tile_height). For a stripped TIFF this is
    /// (width, rows_per_strip).
    fn tile_size(&self) -> (u32, u32);
    /// Decode the chunk at tile column `tx`, tile row `ty`.
    fn read_tile(&mut self, tx: u32, ty: u32) -> Result<Tile>;

    fn tiles_across(&self) -> u32 {
        self.width().div_ceil(self.tile_size().0)
    }
    fn tiles_down(&self) -> u32 {
        self.height().div_ceil(self.tile_size().1)
    }
    /// True for a value that carries data: finite and not the nodata marker.
    fn is_data(&self, v: f32) -> bool {
        v.is_finite()
            && match self.nodata() {
                Some(nd) => v != nd,
                None => true,
            }
    }
}

/// In-memory raster for tests: the whole grid in one Vec, tiled on demand.
#[cfg(test)]
pub struct MemRaster {
    pub width: u32,
    pub height: u32,
    pub transform: GeoTransform,
    pub nodata: Option<f32>,
    pub tile: (u32, u32),
    pub data: Vec<f32>,
}

#[cfg(test)]
impl MemRaster {
    /// The same grid as `tests/fixtures/raster/pop_small_cog.tif`: 40×30,
    /// 0.1° pixels from (3.0, 7.0), value r*100 + c + 1, nodata at
    /// (0,0), (5,5), (29,39), 16×16 tiles.
    pub fn fixture() -> MemRaster {
        let (w, h) = (40u32, 30u32);
        let mut data = Vec::with_capacity((w * h) as usize);
        for r in 0..h {
            for c in 0..w {
                let v = if matches!((r, c), (0, 0) | (5, 5) | (29, 39)) {
                    -99999.0
                } else {
                    (r * 100 + c + 1) as f32
                };
                data.push(v);
            }
        }
        MemRaster {
            width: w,
            height: h,
            transform: GeoTransform {
                x0: 3.0,
                y0: 7.0,
                px: 0.1,
                py: 0.1,
            },
            nodata: Some(-99999.0),
            tile: (16, 16),
            data,
        }
    }
}

#[cfg(test)]
impl Raster for MemRaster {
    fn width(&self) -> u32 {
        self.width
    }
    fn height(&self) -> u32 {
        self.height
    }
    fn transform(&self) -> &GeoTransform {
        &self.transform
    }
    fn nodata(&self) -> Option<f32> {
        self.nodata
    }
    fn tile_size(&self) -> (u32, u32) {
        self.tile
    }
    fn read_tile(&mut self, tx: u32, ty: u32) -> Result<Tile> {
        let (tw, th) = self.tile;
        let (c0, r0) = (tx * tw, ty * th);
        let w = tw.min(self.width - c0);
        let h = th.min(self.height - r0);
        let mut data = Vec::with_capacity((w * h) as usize);
        for r in r0..r0 + h {
            for c in c0..c0 + w {
                data.push(self.data[(r * self.width + c) as usize]);
            }
        }
        Ok(Tile {
            width: w,
            height: h,
            data,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T: GeoTransform = GeoTransform {
        x0: 3.0,
        y0: 7.0,
        px: 0.1,
        py: 0.1,
    };

    #[test]
    fn pixel_and_geo_coordinates_round_trip() {
        assert_eq!(T.col_of(3.0), 0.0);
        assert_eq!(T.row_of(7.0), 0.0);
        assert!((T.col_of(3.25) - 2.5).abs() < 1e-9);
        assert!((T.row_of(6.75) - 2.5).abs() < 1e-9);
        assert!((T.lon_center(0) - 3.05).abs() < 1e-9);
        assert!((T.lat_center(0) - 6.95).abs() < 1e-9);
        assert!((T.lon_center(39) - 6.95).abs() < 1e-9);
        assert!((T.lat_center(29) - 4.05).abs() < 1e-9);
    }

    #[test]
    fn mem_raster_tiles_match_the_fixture_layout() {
        let mut r = MemRaster::fixture();
        assert_eq!((r.tiles_across(), r.tiles_down()), (3, 2));
        let t = r.read_tile(0, 0).unwrap();
        assert_eq!((t.width, t.height), (16, 16));
        assert_eq!(t.data[0], -99999.0);
        assert_eq!(t.data[1], 2.0);
        assert_eq!(t.data[16], 101.0);
        let t = r.read_tile(2, 1).unwrap();
        assert_eq!((t.width, t.height), (8, 14));
        assert_eq!(t.data[0], 1633.0, "row 16, col 32");
        assert_eq!(*t.data.last().unwrap(), -99999.0);
        assert!(!r.is_data(-99999.0));
        assert!(!r.is_data(f32::NAN));
        assert!(r.is_data(0.0));
    }
}
```

Register the module in `src/main.rs`: add `mod raster;` after `mod load;` (alphabetical — after `mod population;` once Task 6 adds it; for now after `mod load;`).

Create empty stubs so the crate compiles: `src/raster/fetch.rs`, `src/raster/geotiff.rs`, `src/raster/zonal.rs`, each containing only a one-line `//!` doc comment for now (the tasks below replace them).

- [ ] **Step 4: Run the tests**

Run: `cargo test raster:: 2>&1 | tail -5`
Expected: `test result: ok. 2 passed`. If clippy complains about dead code on `raster` (nothing uses it yet), that is fine until Task 6; do not add `#[allow(dead_code)]`.

- [ ] **Step 5: Commit**

```bash
git add Cargo.toml Cargo.lock tests/fixtures/raster/pop_small_cog.tif src/raster src/main.rs
git commit -m "raster: GeoTransform, Raster trait, in-memory test raster, COG fixture"
```

---

### Task 2: GeoTIFF reader over the `tiff` crate

**Files:**
- Create: `src/raster/geotiff.rs`

- [ ] **Step 1: Write the failing tests**

`src/raster/geotiff.rs`:

```rust
//! GeoTIFF over an in-memory byte buffer via the pure-Rust `tiff` crate:
//! one band, 32-bit float samples, tiled or stripped, any compression the
//! crate decodes (WorldPop COGs are DEFLATE). The whole file is held in
//! memory: a 100 m country raster is 100–200 MB compressed, which the
//! deployment target has to spare and is far cheaper than one HTTP range
//! request per tile. Only the first IFD (full resolution) is read; COG
//! overviews are ignored.

use std::io::Cursor;

use tiff::decoder::{ChunkType, Decoder, DecodingResult};
use tiff::tags::Tag;

use super::{GeoTransform, Raster, Tile};
use crate::error::{KilnError, Result};

/// GeoKey 1024 (GTModelType): 2 = geographic lat/lon.
const GT_MODEL_TYPE_KEY: u16 = 1024;
const MODEL_TYPE_GEOGRAPHIC: u16 = 2;

pub struct GeoTiff {
    decoder: Decoder<Cursor<Vec<u8>>>,
    width: u32,
    height: u32,
    transform: GeoTransform,
    nodata: Option<f32>,
    tile: (u32, u32),
    label: String,
}

fn tiff_err(label: &str, e: tiff::TiffError) -> KilnError {
    KilnError::Usage(format!("{label}: {e}"))
}

impl GeoTiff {
    /// Parse the header, georeferencing and nodata, and decode the first
    /// tile to confirm the sample type, so a wrong file fails here and not
    /// half way through a run. `label` names the file in errors.
    pub fn open(bytes: Vec<u8>, label: &str) -> Result<GeoTiff> {
        let mut decoder = Decoder::new(Cursor::new(bytes)).map_err(|e| tiff_err(label, e))?;
        let (width, height) = decoder.dimensions().map_err(|e| tiff_err(label, e))?;
        let transform = geotransform(&mut decoder, label)?;
        check_geographic(&mut decoder, label)?;
        let nodata = match decoder
            .find_tag(Tag::GdalNodata)
            .map_err(|e| tiff_err(label, e))?
        {
            Some(v) => {
                let s = v.into_string().map_err(|e| tiff_err(label, e))?;
                let s = s.trim_end_matches('\0').trim().to_string();
                Some(s.parse::<f32>().map_err(|_| {
                    KilnError::Usage(format!("{label}: GDAL_NODATA {s:?} is not a number"))
                })?)
            }
            None => None,
        };
        let tile = decoder.chunk_dimensions();
        let mut g = GeoTiff {
            decoder,
            width,
            height,
            transform,
            nodata,
            tile,
            label: label.to_string(),
        };
        g.read_tile(0, 0)?;
        Ok(g)
    }

    pub fn label(&self) -> &str {
        &self.label
    }
}

/// ModelPixelScale + ModelTiepoint → GeoTransform. A tiepoint elsewhere
/// than pixel (0,0) is folded in. ModelTransformation (rotated / sheared
/// rasters) is not supported: WorldPop grids are north-up.
fn geotransform(decoder: &mut Decoder<Cursor<Vec<u8>>>, label: &str) -> Result<GeoTransform> {
    let f64s = |decoder: &mut Decoder<Cursor<Vec<u8>>>, tag: Tag| -> Result<Option<Vec<f64>>> {
        decoder
            .find_tag(tag)
            .map_err(|e| tiff_err(label, e))?
            .map(|v| v.into_f64_vec().map_err(|e| tiff_err(label, e)))
            .transpose()
    };
    let scale = f64s(decoder, Tag::ModelPixelScaleTag)?;
    let tie = f64s(decoder, Tag::ModelTiepointTag)?;
    let (Some(scale), Some(tie)) = (scale, tie) else {
        return Err(KilnError::Usage(format!(
            "{label}: no ModelPixelScale/ModelTiepoint tags; only north-up GeoTIFFs are supported"
        )));
    };
    if scale.len() < 2 || tie.len() < 6 {
        return Err(KilnError::Usage(format!(
            "{label}: malformed ModelPixelScale ({} values) or ModelTiepoint ({} values)",
            scale.len(),
            tie.len()
        )));
    }
    let (px, py) = (scale[0], scale[1]);
    if !(px > 0.0 && py > 0.0) {
        return Err(KilnError::Usage(format!(
            "{label}: pixel scale must be positive, got ({px}, {py})"
        )));
    }
    Ok(GeoTransform {
        x0: tie[3] - tie[0] * px,
        y0: tie[4] + tie[1] * py,
        px,
        py,
    })
}

/// Refuse a projected raster: its "pixel coordinates" would be metres,
/// and the boundaries are lon/lat. A file without a GeoKeyDirectory is
/// allowed through (nothing to check).
fn check_geographic(decoder: &mut Decoder<Cursor<Vec<u8>>>, label: &str) -> Result<()> {
    let Some(keys) = decoder
        .find_tag_unsigned_vec::<u16>(Tag::GeoKeyDirectoryTag)
        .map_err(|e| tiff_err(label, e))?
    else {
        return Ok(());
    };
    // Header is 4 shorts, then one (key, location, count, value) per entry.
    for entry in keys.chunks_exact(4).skip(1) {
        if entry[0] == GT_MODEL_TYPE_KEY && entry[1] == 0 && entry[3] != MODEL_TYPE_GEOGRAPHIC {
            return Err(KilnError::Usage(format!(
                "{label}: not a geographic (lon/lat) raster (GTModelType = {}); reproject to EPSG:4326 first",
                entry[3]
            )));
        }
    }
    Ok(())
}

impl Raster for GeoTiff {
    fn width(&self) -> u32 {
        self.width
    }
    fn height(&self) -> u32 {
        self.height
    }
    fn transform(&self) -> &GeoTransform {
        &self.transform
    }
    fn nodata(&self) -> Option<f32> {
        self.nodata
    }
    fn tile_size(&self) -> (u32, u32) {
        self.tile
    }
    fn read_tile(&mut self, tx: u32, ty: u32) -> Result<Tile> {
        let across = self.tiles_across();
        if tx >= across || ty >= self.tiles_down() {
            return Err(KilnError::Usage(format!(
                "{}: tile ({tx}, {ty}) is outside the {across}×{} grid",
                self.label,
                self.tiles_down()
            )));
        }
        let index = match self.decoder.get_chunk_type() {
            ChunkType::Tile => ty * across + tx,
            ChunkType::Strip => ty,
        };
        let (width, height) = self.decoder.chunk_data_dimensions(index);
        match self
            .decoder
            .read_chunk(index)
            .map_err(|e| tiff_err(&self.label, e))?
        {
            DecodingResult::F32(data) => {
                if data.len() != (width as usize) * (height as usize) {
                    return Err(KilnError::Environment(format!(
                        "{}: tile {index} decoded to {} values, expected {}×{}",
                        self.label,
                        data.len(),
                        width,
                        height
                    )));
                }
                Ok(Tile {
                    width,
                    height,
                    data,
                })
            }
            other => Err(KilnError::Usage(format!(
                "{}: expected 32-bit float samples, got {}",
                self.label,
                sample_name(&other)
            ))),
        }
    }
}

fn sample_name(r: &DecodingResult) -> &'static str {
    match r {
        DecodingResult::U8(_) => "8-bit unsigned",
        DecodingResult::U16(_) => "16-bit unsigned",
        DecodingResult::U32(_) => "32-bit unsigned",
        DecodingResult::U64(_) => "64-bit unsigned",
        DecodingResult::I8(_) => "8-bit signed",
        DecodingResult::I16(_) => "16-bit signed",
        DecodingResult::I32(_) => "32-bit signed",
        DecodingResult::I64(_) => "64-bit signed",
        DecodingResult::F16(_) => "16-bit float",
        DecodingResult::F32(_) => "32-bit float",
        DecodingResult::F64(_) => "64-bit float",
    }
}

#[cfg(test)]
pub const FIXTURE: &[u8] = include_bytes!("../../tests/fixtures/raster/pop_small_cog.tif");

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> GeoTiff {
        GeoTiff::open(FIXTURE.to_vec(), "pop_small_cog.tif").unwrap()
    }

    #[test]
    fn header_georeferencing_and_nodata_are_read() {
        let g = fixture();
        assert_eq!((g.width(), g.height()), (40, 30));
        assert_eq!(g.tile_size(), (16, 16));
        assert_eq!((g.tiles_across(), g.tiles_down()), (3, 2));
        assert_eq!(g.nodata(), Some(-99999.0));
        let t = g.transform();
        assert!((t.x0 - 3.0).abs() < 1e-12 && (t.y0 - 7.0).abs() < 1e-12);
        assert!((t.px - 0.1).abs() < 1e-12 && (t.py - 0.1).abs() < 1e-12);
        assert_eq!(g.label(), "pop_small_cog.tif");
    }

    #[test]
    fn tiles_decode_with_edge_tiles_trimmed() {
        let mut g = fixture();
        let t = g.read_tile(0, 0).unwrap();
        assert_eq!((t.width, t.height), (16, 16));
        assert_eq!(t.data[0], -99999.0, "nodata at (0,0)");
        assert_eq!(t.data[1], 2.0, "row 0 col 1");
        assert_eq!(t.data[16], 101.0, "row 1 col 0");
        let t = g.read_tile(2, 1).unwrap();
        assert_eq!((t.width, t.height), (8, 14));
        assert_eq!(t.data[0], 1633.0, "row 16 col 32");
        assert_eq!(*t.data.last().unwrap(), -99999.0, "nodata at (29,39)");
        assert!(g.read_tile(3, 0).is_err());
        assert!(g.read_tile(0, 2).is_err());
    }

    #[test]
    fn every_tile_sums_to_the_known_total() {
        let mut g = fixture();
        let (mut sum, mut n) = (0.0f64, 0u64);
        for ty in 0..g.tiles_down() {
            for tx in 0..g.tiles_across() {
                let t = g.read_tile(tx, ty).unwrap();
                assert_eq!(t.data.len(), (t.width * t.height) as usize);
                for &v in &t.data {
                    if g.is_data(v) {
                        sum += f64::from(v);
                        n += 1;
                    }
                }
            }
        }
        assert_eq!((sum, n), (1_761_153.0, 1197));
    }

    #[test]
    fn not_a_tiff_is_a_usage_error() {
        let err = GeoTiff::open(b"not a tiff".to_vec(), "x.tif").unwrap_err();
        assert_eq!(err.exit_code(), 2);
        assert!(err.to_string().starts_with("x.tif:"), "{err}");
    }
}
```

- [ ] **Step 2: Run the tests**

Run: `cargo test raster::geotiff 2>&1 | tail -8`
Expected: `4 passed`. If `find_tag_unsigned_vec::<u16>` fails to compile against the installed `tiff` version, check `cargo doc --open -p tiff` — the signature in 0.11.3 is `fn find_tag_unsigned_vec<T: TryFrom<u64>>(&mut self, tag: Tag) -> TiffResult<Option<Vec<T>>>`.

- [ ] **Step 3: Commit**

```bash
git add src/raster/geotiff.rs
git commit -m "raster: GeoTIFF reader over the tiff crate (tiles, geotransform, nodata, lon/lat check)"
```

---

### Task 3: Zonal sum under the pixel-centroid rule

**Files:**
- Create: `src/raster/zonal.rs`

- [ ] **Step 1: Write the failing tests and the implementation**

`src/raster/zonal.rs`:

```rust
//! Zonal sum under the pixel-centroid rule: a pixel belongs to the polygon
//! its centre falls in. Non-overlapping polygons therefore partition the
//! pixels, and children sum exactly to their parent — the property a
//! denominator hierarchy needs. (pixel's `all_touched` counts every pixel
//! a polygon touches, over-counting along every shared boundary; coverage
//! weighting is exact too but needs polygon∩pixel clipping per edge pixel.)
//!
//! Scanline fill with the even-odd rule over every ring of the geometry
//! (exteriors and holes, every part of a MultiPolygon), one raster row at a
//! time; only the tiles the row's spans touch are decoded, and at most one
//! tile row is held in memory.

use std::collections::HashMap;

use geo::{BoundingRect, Coord, Geometry, LineString, Polygon};

use super::{GeoTransform, Raster, Tile};
use crate::error::{KilnError, Result};

#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct ZonalSum {
    pub sum: f64,
    /// Pixels with data (not nodata) whose centre fell inside.
    pub pixels: u64,
}

/// Every ring of an areal geometry. `None` for anything without area.
fn rings_of(geom: &Geometry<f64>) -> Option<Vec<&LineString<f64>>> {
    fn push<'a>(p: &'a Polygon<f64>, out: &mut Vec<&'a LineString<f64>>) {
        out.push(p.exterior());
        out.extend(p.interiors().iter());
    }
    let mut out = Vec::new();
    match geom {
        Geometry::Polygon(p) => push(p, &mut out),
        Geometry::MultiPolygon(mp) => mp.0.iter().for_each(|p| push(p, &mut out)),
        _ => return None,
    }
    Some(out)
}

/// Longitudes where the horizontal line at `lat` crosses a ring edge, sorted.
/// Half-open rule — an endpoint exactly on the line counts for the edge on
/// one side only — so a vertex on the scanline is never counted twice.
fn crossings(rings: &[&LineString<f64>], lat: f64, out: &mut Vec<f64>) {
    out.clear();
    for ring in rings {
        let pts = &ring.0;
        if pts.len() < 2 {
            continue;
        }
        let mut j = pts.len() - 1;
        for i in 0..pts.len() {
            let (a, b): (Coord<f64>, Coord<f64>) = (pts[i], pts[j]);
            if (a.y > lat) != (b.y > lat) {
                out.push(a.x + (lat - a.y) * (b.x - a.x) / (b.y - a.y));
            }
            j = i;
        }
    }
    out.sort_by(|p, q| p.partial_cmp(q).unwrap_or(std::cmp::Ordering::Equal));
}

/// Inclusive column range whose centres lie in [xa, xb), clipped to the raster.
fn columns(t: &GeoTransform, width: u32, xa: f64, xb: f64) -> Option<(u32, u32)> {
    let start = (t.col_of(xa) - 0.5).ceil().max(0.0);
    let end = ((t.col_of(xb) - 0.5).ceil() - 1.0).min(f64::from(width) - 1.0);
    (end >= start).then(|| (start as u32, end as u32))
}

/// Sum of the raster's data pixels whose centres fall inside `geom`.
pub fn zonal_sum(raster: &mut impl Raster, geom: &Geometry<f64>) -> Result<ZonalSum> {
    let rings = rings_of(geom).ok_or_else(|| {
        KilnError::Usage("zonal sum needs a Polygon or MultiPolygon".to_string())
    })?;
    let Some(rect) = geom.bounding_rect() else {
        return Ok(ZonalSum::default());
    };
    let t = *raster.transform();
    let (tw, th) = raster.tile_size();
    let width = raster.width();
    // Rows whose centres lie within the bbox, clipped to the raster.
    let r0 = (t.row_of(rect.max().y) - 0.5).ceil().max(0.0);
    let r1 = (t.row_of(rect.min().y) - 0.5)
        .floor()
        .min(f64::from(raster.height()) - 1.0);
    if r1 < r0 {
        return Ok(ZonalSum::default());
    }
    let (r0, r1) = (r0 as u32, r1 as u32);

    let mut total = ZonalSum::default();
    let mut xs: Vec<f64> = Vec::new();
    let mut tiles: HashMap<u32, Tile> = HashMap::new();
    let mut current_ty: Option<u32> = None;
    for row in r0..=r1 {
        let ty = row / th;
        if current_ty != Some(ty) {
            tiles.clear();
            current_ty = Some(ty);
        }
        crossings(&rings, t.lat_center(row), &mut xs);
        for pair in xs.chunks_exact(2) {
            let Some((c0, c1)) = columns(&t, width, pair[0], pair[1]) else {
                continue;
            };
            for col in c0..=c1 {
                let tx = col / tw;
                if !tiles.contains_key(&tx) {
                    let tile = raster.read_tile(tx, ty)?;
                    tiles.insert(tx, tile);
                }
                let tile = &tiles[&tx];
                let v = tile.data[((row - ty * th) * tile.width + (col - tx * tw)) as usize];
                if raster.is_data(v) {
                    total.sum += f64::from(v);
                    total.pixels += 1;
                }
            }
        }
    }
    Ok(total)
}

/// Sum of every data pixel in the raster — the figure the per-unit sums
/// should approach when the units tile the country.
pub fn raster_total(raster: &mut impl Raster) -> Result<ZonalSum> {
    let mut total = ZonalSum::default();
    for ty in 0..raster.tiles_down() {
        for tx in 0..raster.tiles_across() {
            let tile = raster.read_tile(tx, ty)?;
            for &v in &tile.data {
                if raster.is_data(v) {
                    total.sum += f64::from(v);
                    total.pixels += 1;
                }
            }
        }
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raster::MemRaster;
    use geo::{polygon, MultiPolygon};

    fn boxed(x0: f64, y0: f64, x1: f64, y1: f64) -> Geometry<f64> {
        Geometry::Polygon(polygon![(x: x0, y: y1), (x: x1, y: y1), (x: x1, y: y0), (x: x0, y: y0), (x: x0, y: y1)])
    }

    fn sum(geom: &Geometry<f64>) -> (f64, u64) {
        let z = zonal_sum(&mut MemRaster::fixture(), geom).unwrap();
        (z.sum, z.pixels)
    }

    #[test]
    fn the_whole_raster_and_an_oversized_polygon_give_the_total() {
        assert_eq!(sum(&boxed(3.0, 4.0, 7.0, 7.0)), (1_761_153.0, 1197));
        assert_eq!(sum(&boxed(-10.0, -10.0, 50.0, 50.0)), (1_761_153.0, 1197));
        let t = raster_total(&mut MemRaster::fixture()).unwrap();
        assert_eq!((t.sum, t.pixels), (1_761_153.0, 1197));
    }

    #[test]
    fn a_small_square_counts_only_pixels_whose_centre_is_inside() {
        // 5×5 pixels, one of them nodata.
        assert_eq!(sum(&boxed(3.0, 6.5, 3.5, 7.0)), (5_074.0, 24));
    }

    #[test]
    fn adjacent_polygons_partition_the_pixels() {
        let left = sum(&boxed(3.0, 4.0, 5.0, 7.0));
        let right = sum(&boxed(5.0, 4.0, 7.0, 7.0));
        assert_eq!(left, (875_793.0, 598));
        assert_eq!(right, (885_360.0, 599));
        assert_eq!(left.0 + right.0, 1_761_153.0);
        assert_eq!(left.1 + right.1, 1197);
    }

    #[test]
    fn a_triangle_uses_sloped_crossings() {
        let tri = Geometry::Polygon(polygon![(x: 3.0, y: 7.0), (x: 7.0, y: 7.0), (x: 3.0, y: 4.0), (x: 3.0, y: 7.0)]);
        assert_eq!(sum(&tri), (578_783.0, 598));
    }

    #[test]
    fn a_polygon_across_four_tiles_reads_each_tile_once_per_tile_row() {
        assert_eq!(sum(&boxed(4.5, 5.2, 5.9, 6.7)), (214_725.0, 210));
    }

    #[test]
    fn a_hole_is_subtracted_and_a_multipolygon_is_summed() {
        let outer = polygon![(x: 3.0, y: 7.0), (x: 7.0, y: 7.0), (x: 7.0, y: 4.0), (x: 3.0, y: 4.0), (x: 3.0, y: 7.0)];
        let hole = polygon![(x: 3.0, y: 7.0), (x: 3.5, y: 7.0), (x: 3.5, y: 6.5), (x: 3.0, y: 6.5), (x: 3.0, y: 7.0)];
        let with_hole = Geometry::Polygon(geo::Polygon::new(
            outer.exterior().clone(),
            vec![hole.exterior().clone()],
        ));
        assert_eq!(sum(&with_hole), (1_756_079.0, 1173));

        let left = polygon![(x: 3.0, y: 7.0), (x: 5.0, y: 7.0), (x: 5.0, y: 4.0), (x: 3.0, y: 4.0), (x: 3.0, y: 7.0)];
        let right = polygon![(x: 5.0, y: 7.0), (x: 7.0, y: 7.0), (x: 7.0, y: 4.0), (x: 5.0, y: 4.0), (x: 5.0, y: 7.0)];
        let mp = Geometry::MultiPolygon(MultiPolygon(vec![left, right]));
        assert_eq!(sum(&mp), (1_761_153.0, 1197));
    }

    #[test]
    fn outside_the_raster_is_zero_and_a_line_is_a_usage_error() {
        assert_eq!(sum(&boxed(20.0, 20.0, 21.0, 21.0)), (0.0, 0));
        let line = Geometry::LineString(LineString::from(vec![(3.0, 4.0), (7.0, 7.0)]));
        let err = zonal_sum(&mut MemRaster::fixture(), &line).unwrap_err();
        assert_eq!(err.exit_code(), 2);
    }

    #[test]
    fn the_real_cog_agrees_with_the_in_memory_grid() {
        use crate::raster::geotiff::{GeoTiff, FIXTURE};
        let mut g = GeoTiff::open(FIXTURE.to_vec(), "fixture").unwrap();
        let z = zonal_sum(&mut g, &boxed(3.0, 4.0, 5.0, 7.0)).unwrap();
        assert_eq!((z.sum, z.pixels), (875_793.0, 598));
        let z = zonal_sum(&mut g, &boxed(4.5, 5.2, 5.9, 6.7)).unwrap();
        assert_eq!((z.sum, z.pixels), (214_725.0, 210));
    }
}
```

- [ ] **Step 2: Run the tests**

Run: `cargo test raster::zonal 2>&1 | tail -12`
Expected: `8 passed`. If a number is off by a pixel, the culprit is almost always `columns()` — `[xa, xb)` must be half-open (`ceil(col−0.5)` for the start, `ceil(col−0.5) − 1` for the end); do not "fix" it by switching to `round()`.

- [ ] **Step 3: Commit**

```bash
git add src/raster/zonal.rs
git commit -m "raster: pixel-centroid zonal sum (scanline even-odd, tile-row streaming) and raster_total"
```

---

### Task 4: Fetching the raster (local path or cached URL)

**Files:**
- Modify: `src/snapshot/mod.rs:15-61` and its `paths_are_under_the_snapshot_dir` test
- Create: `src/raster/fetch.rs`

- [ ] **Step 1: Snapshot path helper**

In `src/snapshot/mod.rs` add after `EXTRACT_REPORT_FILE`:

```rust
pub const RASTERS_DIR: &str = "rasters";
```

and to `impl Snapshot`:

```rust
    /// Content-addressed cache of fetched rasters (`kiln population --raster URL`).
    pub fn rasters(&self) -> PathBuf {
        self.dir.join(RASTERS_DIR)
    }
```

Extend the existing test `paths_are_under_the_snapshot_dir` with:

```rust
        assert_eq!(s.rasters(), std::path::PathBuf::from("/x/rasters"));
```

Run: `cargo test snapshot::tests 2>&1 | tail -3` → all pass.

- [ ] **Step 2: Write the failing fetch tests and implementation**

`src/raster/fetch.rs`:

```rust
//! Where the raster comes from: a local file, or an http(s) URL fetched once
//! into `SNAPSHOT/rasters/` through the same content-addressed `Cache` the
//! boundary fetch uses (sha256 of the URL; bytes verified on read), so
//! re-runs and other years never re-download a country raster.

use std::path::Path;
use std::time::Duration;

use crate::error::{KilnError, Result};
use crate::extract::cache::Cache;
use crate::extract::client::FhirClient;

pub struct RasterBytes {
    pub bytes: Vec<u8>,
    /// File name or last URL segment: for error messages and provenance text.
    pub label: String,
    /// Served from the cache rather than fetched or read.
    pub cached: bool,
}

pub fn is_url(source: &str) -> bool {
    source.starts_with("http://") || source.starts_with("https://")
}

pub fn label_of(source: &str) -> String {
    source
        .rsplit(['/', '\\'])
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or(source)
        .to_string()
}

/// Read `source` into memory. A URL goes through `cache_dir`; cache faults
/// are printed and treated as a miss, never as data.
pub fn load(source: &str, cache_dir: &Path, retries: usize, timeout: Duration) -> Result<RasterBytes> {
    let label = label_of(source);
    if !is_url(source) {
        let path = Path::new(source);
        let bytes = std::fs::read(path).map_err(|e| KilnError::io(path, e))?;
        return Ok(RasterBytes {
            bytes,
            label,
            cached: false,
        });
    }
    let cache = Cache::new(cache_dir);
    match cache.read(source) {
        Ok(Some(bytes)) => {
            return Ok(RasterBytes {
                bytes,
                label,
                cached: true,
            })
        }
        Ok(None) => {}
        Err(e) => eprintln!("kiln: raster cache: {e}; fetching again"),
    }
    let client = FhirClient::new(None, retries, timeout)?;
    let fetched = client
        .get(source)
        .map_err(|e| KilnError::Environment(format!("{source}: {e}")))?;
    if let Err(e) = cache.write(source, &fetched.body) {
        eprintln!("kiln: raster cache: {e}; continuing without caching");
    }
    Ok(RasterBytes {
        bytes: fetched.body,
        label,
        cached: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raster::geotiff::FIXTURE;
    use httptest::{matchers::*, responders::*, Expectation, Server};

    #[test]
    fn labels_are_the_last_path_segment() {
        assert_eq!(label_of("https://h/x/nga_pop_2026_CN_100m_cog.tif"), "nga_pop_2026_CN_100m_cog.tif");
        assert_eq!(label_of("/data/a.tif"), "a.tif");
        assert_eq!(label_of("a.tif"), "a.tif");
        assert!(is_url("https://h/a.tif") && is_url("http://h/a.tif") && !is_url("/h/a.tif"));
    }

    #[test]
    fn a_local_file_is_read_directly() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("pop.tif");
        std::fs::write(&path, FIXTURE).unwrap();
        let got = load(path.to_str().unwrap(), &dir.path().join("cache"), 1, Duration::from_secs(5)).unwrap();
        assert_eq!(got.bytes, FIXTURE);
        assert_eq!(got.label, "pop.tif");
        assert!(!got.cached);
        assert!(!dir.path().join("cache").exists(), "no cache for local files");
    }

    #[test]
    fn a_url_is_fetched_once_then_served_from_the_cache() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/nga.tif"))
                .times(1)
                .respond_with(status_code(200).body(FIXTURE.to_vec())),
        );
        let url = server.url("/nga.tif").to_string();
        let dir = tempfile::tempdir().unwrap();
        let cache = dir.path().join("rasters");

        let first = load(&url, &cache, 1, Duration::from_secs(5)).unwrap();
        assert_eq!(first.bytes, FIXTURE);
        assert_eq!(first.label, "nga.tif");
        assert!(!first.cached);
        assert_eq!(std::fs::read_dir(&cache).unwrap().count(), 2, ".bin and .meta.json");

        let second = load(&url, &cache, 1, Duration::from_secs(5)).unwrap();
        assert_eq!(second.bytes, FIXTURE);
        assert!(second.cached);
    }

    #[test]
    fn a_missing_url_is_an_environment_error() {
        let server = Server::run();
        server.expect(
            Expectation::matching(request::method_path("GET", "/none.tif"))
                .respond_with(status_code(404)),
        );
        let err = load(&server.url("/none.tif").to_string(), &tempfile::tempdir().unwrap().path().join("c"), 1, Duration::from_secs(5)).unwrap_err();
        assert_eq!(err.exit_code(), 1);
        assert!(err.to_string().contains("404"), "{err}");
    }

    #[test]
    fn a_missing_local_file_is_an_io_error_naming_the_path() {
        let err = load("/definitely/not/here.tif", Path::new("/unused"), 1, Duration::from_secs(5)).unwrap_err();
        assert!(err.to_string().contains("/definitely/not/here.tif"), "{err}");
    }
}
```

- [ ] **Step 3: Run the tests**

Run: `cargo test raster::fetch 2>&1 | tail -8`
Expected: `5 passed`. (`httptest`'s `status_code(200).body(Vec<u8>)` is the same responder family `extract.rs` tests use; `request::method_path` is in `httptest::matchers`.)

- [ ] **Step 4: Commit**

```bash
git add src/snapshot/mod.rs src/raster/fetch.rs
git commit -m "raster: load --raster from a local path or a URL cached under SNAPSHOT/rasters"
```

---

### Task 5: The `ICRTargetPopulation` Group

**Files:**
- Create: `src/fhir/group.rs`
- Modify: `src/fhir/mod.rs`

- [ ] **Step 1: Register the module**

`src/fhir/mod.rs`: add `pub mod group;` after `pub mod location;` (keep alphabetical: `group`, `location`, `ndjson`, `organization`, `spatial`).

- [ ] **Step 2: Write the failing tests and implementation**

`src/fhir/group.rs`:

```rust
//! The ICR `ICRTargetPopulation` profile on `Group`: a conceptual cohort
//! (`actual = false`) with a head count, scoped to a Location through the
//! `geography` characteristic, with source and date provenance in
//! extensions. Population is never a Location attribute in the ICR (the
//! georegistry rule: Location holds identity and place; revisable
//! programmatic figures live beside it), so `kiln population` emits these
//! and leaves the Location untouched. As with `location.rs`, every
//! profile-specific URL is a constant here and nowhere else.

use serde_json::{json, Value};

pub const TARGET_POPULATION_PROFILE: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/ICRTargetPopulation";
pub const GROUP_CHARACTERISTIC_SYSTEM: &str =
    "https://icr.healthcampaigns.org/CodeSystem/icr-group-characteristic-cs";
pub const DENOMINATOR_SOURCE_SYSTEM: &str =
    "https://icr.healthcampaigns.org/CodeSystem/icr-denominator-source-cs";
pub const DENOMINATOR_TYPE_SYSTEM: &str =
    "https://icr.healthcampaigns.org/CodeSystem/icr-denominator-type-cs";
pub const DENOMINATOR_SOURCE_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/denominator-source";
pub const DENOMINATOR_TYPE_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/denominator-type";
pub const ESTIMATE_DATE_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/estimate-date";
pub const IS_PLANNING_DENOMINATOR_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/is-planning-denominator";
pub const IS_CALCULATED_EXTENSION_URL: &str =
    "https://icr.healthcampaigns.org/StructureDefinition/is-calculated";

/// FHIR `id` is `[A-Za-z0-9\-\.]{1,64}`.
pub const MAX_ID_LEN: usize = 64;

/// Display text for the `icr-denominator-source-cs` codes kiln emits.
pub fn source_display(code: &str) -> Option<&'static str> {
    match code {
        "worldpop" => Some("WorldPop modelled estimate"),
        _ => None,
    }
}

/// `pop-{source}-{year}-{locationId}`: deterministic, so a re-run replaces
/// the same resources.
pub fn group_id(source_code: &str, year: u16, location_id: &str) -> String {
    format!("pop-{source_code}-{year}-{location_id}")
}

pub struct TargetPopulation<'a> {
    pub location_id: &'a str,
    pub location_name: Option<&'a str>,
    pub count: u64,
    pub year: u16,
    pub source_code: &'a str,
    /// Free-text provenance: raster name and method.
    pub source_text: &'a str,
    /// Rolled up from children rather than measured directly.
    pub calculated: bool,
    pub planning_denominator: bool,
}

pub fn target_population_group(p: &TargetPopulation) -> Value {
    let scope = p.location_name.unwrap_or(p.location_id);
    let display = source_display(p.source_code);
    let mut coding = json!({"system": DENOMINATOR_SOURCE_SYSTEM, "code": p.source_code});
    if let Some(d) = display {
        coding["display"] = Value::String(d.to_string());
    }
    let mut group = json!({
        "resourceType": "Group",
        "id": group_id(p.source_code, p.year, p.location_id),
        "meta": {"profile": [TARGET_POPULATION_PROFILE]},
        "type": "person",
        "actual": false,
        "name": format!("Total population, {scope}, {} ({})", p.year, display.unwrap_or(p.source_code)),
        "quantity": p.count,
        "characteristic": [{
            "code": {"coding": [{"system": GROUP_CHARACTERISTIC_SYSTEM, "code": "geography", "display": "Geographic scope"}]},
            "valueReference": {"reference": format!("Location/{}", p.location_id), "display": scope},
            "exclude": false
        }],
        "extension": [
            {"url": DENOMINATOR_SOURCE_EXTENSION_URL,
             "valueCodeableConcept": {"coding": [coding], "text": p.source_text}},
            {"url": DENOMINATOR_TYPE_EXTENSION_URL,
             "valueCodeableConcept": {"coding": [{"system": DENOMINATOR_TYPE_SYSTEM, "code": "total-population", "display": "Total population"}]}},
            {"url": ESTIMATE_DATE_EXTENSION_URL, "valueDate": format!("{:04}-01-01", p.year)},
            {"url": IS_CALCULATED_EXTENSION_URL, "valueBoolean": p.calculated},
        ]
    });
    if p.planning_denominator {
        group["extension"]
            .as_array_mut()
            .expect("extension is an array")
            .push(json!({"url": IS_PLANNING_DENOMINATOR_EXTENSION_URL, "valueBoolean": true}));
    }
    group
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(planning: bool) -> Value {
        target_population_group(&TargetPopulation {
            location_id: "lga-1",
            location_name: Some("Kano Municipal"),
            count: 512_345,
            year: 2026,
            source_code: "worldpop",
            source_text: "nga_pop_2026_CN_100m_cog.tif; pixel-centroid zonal sum over admin level 2",
            calculated: false,
            planning_denominator: planning,
        })
    }

    fn ext<'a>(g: &'a Value, url: &str) -> Option<&'a Value> {
        g["extension"].as_array().unwrap().iter().find(|e| e["url"] == url)
    }

    #[test]
    fn matches_the_icr_target_population_profile_shape() {
        let g = sample(false);
        assert_eq!(g["resourceType"], "Group");
        assert_eq!(g["id"], "pop-worldpop-2026-lga-1");
        assert_eq!(g["meta"]["profile"][0], TARGET_POPULATION_PROFILE);
        assert_eq!(g["type"], "person");
        assert_eq!(g["actual"], false);
        assert_eq!(g["quantity"], 512_345);
        assert_eq!(g["name"], "Total population, Kano Municipal, 2026 (WorldPop modelled estimate)");
        let c = &g["characteristic"][0];
        assert_eq!(c["code"]["coding"][0]["system"], GROUP_CHARACTERISTIC_SYSTEM);
        assert_eq!(c["code"]["coding"][0]["code"], "geography");
        assert_eq!(c["valueReference"]["reference"], "Location/lga-1");
        assert_eq!(c["valueReference"]["display"], "Kano Municipal");
        assert_eq!(c["exclude"], false);
        let src = ext(&g, DENOMINATOR_SOURCE_EXTENSION_URL).unwrap();
        assert_eq!(src["valueCodeableConcept"]["coding"][0]["system"], DENOMINATOR_SOURCE_SYSTEM);
        assert_eq!(src["valueCodeableConcept"]["coding"][0]["code"], "worldpop");
        assert_eq!(src["valueCodeableConcept"]["coding"][0]["display"], "WorldPop modelled estimate");
        assert!(src["valueCodeableConcept"]["text"].as_str().unwrap().contains("nga_pop_2026"));
        assert_eq!(ext(&g, DENOMINATOR_TYPE_EXTENSION_URL).unwrap()["valueCodeableConcept"]["coding"][0]["code"], "total-population");
        assert_eq!(ext(&g, ESTIMATE_DATE_EXTENSION_URL).unwrap()["valueDate"], "2026-01-01");
        assert_eq!(ext(&g, IS_CALCULATED_EXTENSION_URL).unwrap()["valueBoolean"], false);
        assert!(ext(&g, IS_PLANNING_DENOMINATOR_EXTENSION_URL).is_none());
    }

    #[test]
    fn planning_flag_adds_the_extension_and_unknown_sources_have_no_display() {
        let g = sample(true);
        assert_eq!(ext(&g, IS_PLANNING_DENOMINATOR_EXTENSION_URL).unwrap()["valueBoolean"], true);

        let g = target_population_group(&TargetPopulation {
            location_id: "x",
            location_name: None,
            count: 1,
            year: 2027,
            source_code: "grid3",
            source_text: "t",
            calculated: true,
            planning_denominator: false,
        });
        assert_eq!(g["id"], "pop-grid3-2027-x");
        assert_eq!(g["name"], "Total population, x, 2027 (grid3)");
        let coding = &ext(&g, DENOMINATOR_SOURCE_EXTENSION_URL).unwrap()["valueCodeableConcept"]["coding"][0];
        assert!(coding.get("display").is_none());
        assert_eq!(ext(&g, IS_CALCULATED_EXTENSION_URL).unwrap()["valueBoolean"], true);
    }
}
```

- [ ] **Step 3: Run the tests**

Run: `cargo test fhir::group 2>&1 | tail -5` → `2 passed`.

- [ ] **Step 4: Commit**

```bash
git add src/fhir/mod.rs src/fhir/group.rs
git commit -m "fhir: ICRTargetPopulation Group builder with deterministic ids"
```

---

### Task 6: The `population` command

**Files:**
- Modify: `src/cli.rs:26-42` (enum) and append `PopulationArgs`
- Modify: `src/main.rs`
- Create: `src/population.rs`

- [ ] **Step 1: CLI args**

In `src/cli.rs`, add to `enum Command` after `Index(IndexArgs)`:

```rust
    /// Population denominators: sum a WorldPop-style raster under every admin unit at one level, roll the sums up the hierarchy, and write ICRTargetPopulation Groups as FHIR NDJSON for `kiln load` (offline once the raster is cached)
    Population(PopulationArgs),
```

and append after `IndexArgs`:

```rust
#[derive(clap::Args, Debug, Clone)]
pub struct PopulationArgs {
    /// Snapshot directory containing locations.ndjson
    #[arg(long)]
    pub snapshot: PathBuf,
    /// GeoTIFF to sum: a local path, or an http(s) URL fetched once into the raster cache
    #[arg(long)]
    pub raster: String,
    /// Admin level to measure from the raster (0 = country); every admin ancestor gets a rolled-up total
    #[arg(long)]
    pub level: u8,
    /// Year the raster estimates; becomes each Group's estimate-date and part of its id
    #[arg(long)]
    pub year: u16,
    /// denominator-source code (icr-denominator-source-cs)
    #[arg(long, default_value = "worldpop")]
    pub source: String,
    /// Mark every written Group as the planning denominator
    #[arg(long)]
    pub planning_denominator: bool,
    /// Output NDJSON file of Group resources
    #[arg(long)]
    pub out: PathBuf,
    /// Also write the report as JSON to this file
    #[arg(long)]
    pub report: Option<PathBuf>,
    /// Raster cache directory (default: SNAPSHOT/rasters)
    #[arg(long)]
    pub cache_dir: Option<PathBuf>,
    /// Attempts per HTTP request
    #[arg(long, default_value_t = DEFAULT_RETRIES)]
    pub retries: usize,
    /// Total timeout per HTTP request, in seconds
    #[arg(long, default_value_t = DEFAULT_TIMEOUT_SECS)]
    pub timeout: u64,
}
```

In `src/main.rs`: add `mod population;` (after `mod load;`) and `mod raster;` (after `mod population;`, if not already present from Task 1 — keep the list alphabetical), and the match arm after `Index`:

```rust
        cli::Command::Population(args) => population::run_population(&args),
```

- [ ] **Step 2: Write the command with its unit test for the rollup**

`src/population.rs`:

```rust
//! `kiln population`: population denominators for admin units from a raster.
//!
//! Reads the snapshot with the same pass-one index `transform` uses, so the
//! hierarchy (and its report) come for free; sums the raster under every
//! admin unit at `--level` with the pixel-centroid rule; rolls the sums up
//! the partOf chain so every admin ancestor gets a calculated total; and
//! writes one `ICRTargetPopulation` Group per unit as FHIR NDJSON for
//! `kiln load`. Group ids are deterministic (`pop-SOURCE-YEAR-LOCATIONID`),
//! so re-running replaces the same resources. Locations are never touched.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::time::Duration;

use crate::cli::PopulationArgs;
use crate::error::{KilnError, Result};
use crate::fhir::group::{group_id, target_population_group, TargetPopulation, MAX_ID_LEN};
use crate::fhir::ndjson::LineAccess;
use crate::fhir::{Boundary, Location};
use crate::geometry::parse_boundary;
use crate::index::hierarchy::Hierarchy;
use crate::index::{build_index, IndexRecord};
use crate::raster::fetch;
use crate::raster::geotiff::GeoTiff;
use crate::raster::zonal::{raster_total, zonal_sum, ZonalSum};
use crate::report::Report;
use crate::snapshot::{Snapshot, LOCATIONS_FILE};
use crate::transform::write_report;

/// One unit's figure: measured from the raster at `--level`, or rolled up
/// from measured descendants.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct UnitTotal {
    pub sum: f64,
    pub pixels: u64,
    /// Rolled up from children rather than measured directly.
    pub calculated: bool,
    /// A measured descendant had no usable boundary, so this under-counts.
    pub incomplete: bool,
}

/// Admin-unit ancestors of record `i`, nearest first. Non-admin nodes on
/// the chain (a facility's supervisory area, say) are skipped, not stopped at.
fn admin_ancestors(hierarchy: &Hierarchy, i: usize) -> Vec<usize> {
    let mut out = Vec::new();
    let mut cur = hierarchy.get(i).and_then(|info| info.parent);
    while let Some(n) = cur {
        let idx = n.index();
        let info = hierarchy.get(idx);
        if info.and_then(|f| f.admin_level).is_some() {
            out.push(idx);
        }
        cur = info.and_then(|f| f.parent);
    }
    out
}

/// Fold one measured unit (or a unit that could not be measured) into every
/// admin ancestor's rolled-up total.
pub fn roll_up(
    totals: &mut BTreeMap<usize, UnitTotal>,
    hierarchy: &Hierarchy,
    i: usize,
    measured: Option<ZonalSum>,
) {
    for anc in admin_ancestors(hierarchy, i) {
        let t = totals.entry(anc).or_insert(UnitTotal {
            calculated: true,
            ..UnitTotal::default()
        });
        match measured {
            Some(z) => {
                t.sum += z.sum;
                t.pixels += z.pixels;
            }
            None => t.incomplete = true,
        }
    }
}

/// The unit's boundary summed against the raster; `None` (after reporting)
/// when there is nothing usable to measure. Parsing here re-reads the line
/// pass one already indexed, so parse noise goes to a scratch report and
/// only this command's own kinds reach `report`.
fn measure(
    lines: &mut LineAccess,
    rec: &IndexRecord,
    raster: &mut GeoTiff,
    report: &mut Report,
) -> Result<Option<ZonalSum>> {
    let text = lines.read_at(rec.offset, rec.len)?;
    let value: serde_json::Value = serde_json::from_str(&text)?;
    let mut scratch = Report::default();
    let Some(loc) = Location::parse(&value, &mut scratch) else {
        return Ok(None);
    };
    let geom = match &loc.boundary {
        Some(Boundary::Inline(bytes)) => {
            let g = parse_boundary(bytes, &loc.id, &mut scratch);
            if g.is_none() {
                report.add("no_boundary", &loc.id, "boundary did not parse; no population measured");
            }
            g
        }
        Some(Boundary::Url(url)) => {
            report.add(
                "no_boundary",
                &loc.id,
                &format!("boundary is an unresolved url ({url}); run extract to inline it"),
            );
            None
        }
        None => {
            report.add("no_boundary", &loc.id, "admin unit has no boundary; no population measured");
            None
        }
    };
    let Some(geom) = geom else {
        return Ok(None);
    };
    match zonal_sum(raster, &geom) {
        Ok(z) => Ok(Some(z)),
        Err(KilnError::Usage(msg)) => {
            report.add("no_boundary", &loc.id, &format!("{msg}; no population measured"));
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

fn valid_source_code(code: &str) -> bool {
    !code.is_empty() && code.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

pub fn run_population(args: &PopulationArgs) -> Result<()> {
    let snapshot = Snapshot::new(&args.snapshot);
    let ndjson = snapshot.locations();
    if !ndjson.is_file() {
        return Err(KilnError::Usage(format!(
            "snapshot file not found: {}",
            ndjson.display()
        )));
    }
    if args.out.is_dir() {
        return Err(KilnError::Usage(format!(
            "--out {}: is a directory",
            args.out.display()
        )));
    }
    if !valid_source_code(&args.source) {
        return Err(KilnError::Usage(format!(
            "--source must be a code of letters, digits and hyphens, got {:?}",
            args.source
        )));
    }

    let index = build_index(&ndjson, None)?;
    let mut report = index.report;
    let level = i8::try_from(args.level)
        .map_err(|_| KilnError::Usage(format!("--level {} is out of range", args.level)))?;
    let targets: Vec<usize> = (0..index.records.len())
        .filter(|&i| index.hierarchy.get(i).and_then(|f| f.admin_level) == Some(level))
        .collect();
    if targets.is_empty() {
        return Err(KilnError::Usage(format!(
            "no admin units at level {} in {}",
            args.level,
            ndjson.display()
        )));
    }

    let cache_dir = args.cache_dir.clone().unwrap_or_else(|| snapshot.rasters());
    let loaded = fetch::load(
        &args.raster,
        &cache_dir,
        args.retries,
        Duration::from_secs(args.timeout),
    )?;
    println!(
        "Raster {} ({:.1} MB{})",
        loaded.label,
        loaded.bytes.len() as f64 / 1e6,
        if loaded.cached { ", from cache" } else { "" }
    );
    let mut raster = GeoTiff::open(loaded.bytes, &loaded.label)?;
    let grand = raster_total(&mut raster)?;

    let mut lines = LineAccess::open(&ndjson)?;
    let mut totals: BTreeMap<usize, UnitTotal> = BTreeMap::new();
    for &i in &targets {
        let measured = measure(&mut lines, &index.records[i], &mut raster, &mut report)?;
        if let Some(z) = measured {
            totals.insert(
                i,
                UnitTotal {
                    sum: z.sum,
                    pixels: z.pixels,
                    calculated: false,
                    incomplete: false,
                },
            );
        }
        roll_up(&mut totals, &index.hierarchy, i, measured);
    }

    let source_text = format!(
        "{}; pixel-centroid zonal sum over admin level {}",
        loaded.label, args.level
    );
    let tmp = args.out.with_extension("ndjson.tmp");
    let file = File::create(&tmp).map_err(|e| KilnError::io(&tmp, e))?;
    let mut out = BufWriter::new(file);
    let (mut measured_n, mut rolled_n) = (0usize, 0usize);
    for (&i, t) in &totals {
        let rec = &index.records[i];
        if t.pixels == 0 {
            report.add(
                if t.calculated { "rollup_empty" } else { "no_pixels" },
                &rec.id,
                "no raster pixel with data has its centre inside; no Group written",
            );
            continue;
        }
        if t.incomplete {
            report.add(
                "rollup_incomplete",
                &rec.id,
                "a descendant at the measured level had no usable boundary; this total under-counts",
            );
        }
        let id = group_id(&args.source, args.year, &rec.id);
        if id.len() > MAX_ID_LEN {
            report.add(
                "group_id_too_long",
                &rec.id,
                &format!("{id} exceeds {MAX_ID_LEN} characters; no Group written"),
            );
            continue;
        }
        let group = target_population_group(&TargetPopulation {
            location_id: &rec.id,
            location_name: rec.name.as_deref(),
            count: t.sum.round() as u64,
            year: args.year,
            source_code: &args.source,
            source_text: &source_text,
            calculated: t.calculated,
            planning_denominator: args.planning_denominator,
        });
        serde_json::to_writer(&mut out, &group)?;
        out.write_all(b"\n").map_err(|e| KilnError::io(&tmp, e))?;
        if t.calculated {
            rolled_n += 1;
        } else {
            measured_n += 1;
        }
    }
    out.flush().map_err(|e| KilnError::io(&tmp, e))?;
    drop(out);
    std::fs::rename(&tmp, &args.out).map_err(|e| KilnError::io(&args.out, e))?;

    let assigned: f64 = totals.values().filter(|t| !t.calculated).map(|t| t.sum).sum();
    let share = if grand.sum > 0.0 { 100.0 * assigned / grand.sum } else { 0.0 };
    println!(
        "Wrote {} Groups to {} ({measured_n} measured at level {}, {rolled_n} rolled up)",
        measured_n + rolled_n,
        args.out.display(),
        args.level
    );
    println!(
        "Raster total {:.0}; assigned to level-{} units {:.0} ({share:.1}%)",
        grand.sum, args.level, assigned
    );
    println!("{}", report.summary());
    if let Some(path) = &args.report {
        write_report(path, &report)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::hierarchy::resolve_hierarchy;

    fn admin(id: &str, part_of: Option<&str>) -> IndexRecord {
        IndexRecord {
            id: id.to_string(),
            part_of: part_of.map(str::to_string),
            type_code: Some("admin-unit".to_string()),
            ..IndexRecord::default()
        }
    }

    #[test]
    fn roll_up_sums_into_every_admin_ancestor_and_flags_gaps() {
        // ng ← kano ← (lga-a, lga-b); ng ← lagos ← lga-c ; clinic (not admin) under lga-a
        let records = vec![
            admin("ng", None),
            admin("kano", Some("ng")),
            admin("lga-a", Some("kano")),
            admin("lga-b", Some("kano")),
            admin("lagos", Some("ng")),
            admin("lga-c", Some("lagos")),
            IndexRecord {
                id: "clinic".into(),
                part_of: Some("lga-a".into()),
                type_code: Some("facility".into()),
                ..IndexRecord::default()
            },
        ];
        let mut report = Report::default();
        let hierarchy = resolve_hierarchy(&records, &mut report);
        assert_eq!(hierarchy.get(2).unwrap().admin_level, Some(2));
        assert_eq!(hierarchy.get(6).unwrap().admin_level, None);

        let mut totals = BTreeMap::new();
        let z = |sum: f64, pixels: u64| Some(ZonalSum { sum, pixels });
        roll_up(&mut totals, &hierarchy, 2, z(10.0, 1));
        roll_up(&mut totals, &hierarchy, 3, z(20.0, 2));
        roll_up(&mut totals, &hierarchy, 5, None);

        let kano = totals[&1];
        assert_eq!((kano.sum, kano.pixels, kano.calculated, kano.incomplete), (30.0, 3, true, false));
        let lagos = totals[&4];
        assert_eq!((lagos.sum, lagos.pixels, lagos.calculated, lagos.incomplete), (0.0, 0, true, true));
        let ng = totals[&0];
        assert_eq!((ng.sum, ng.pixels, ng.calculated, ng.incomplete), (30.0, 3, true, true));
        assert!(!totals.contains_key(&2), "measured units are the caller's to insert");
        assert!(!totals.contains_key(&6));
    }

    #[test]
    fn source_codes_are_restricted_to_id_safe_characters() {
        assert!(valid_source_code("worldpop") && valid_source_code("grid3") && valid_source_code("census-projection"));
        assert!(!valid_source_code("") && !valid_source_code("world pop") && !valid_source_code("a/b"));
    }
}
```

`resolve_hierarchy(records: &[IndexRecord], report: &mut Report) -> Hierarchy` is `pub` in `src/index/hierarchy.rs:362`.

- [ ] **Step 3: Build, run unit tests, run clippy**

Run: `cargo test population:: 2>&1 | tail -5` → `2 passed`.
Run: `cargo clippy --all-targets -- -D warnings 2>&1 | tail -5` → clean. Likely nits: `f64::from(v)` vs `as f64` (use `f64::from` for `f32`/`u32`), needless borrows. Fix them; do not allow-list.
Run: `cargo run -- population --help | head -20` → the new flags print.

- [ ] **Step 4: Commit**

```bash
git add src/cli.rs src/main.rs src/population.rs
git commit -m "population: kiln population — measure admin units at a level, roll up, emit ICRTargetPopulation Groups"
```

---

### Task 7: End-to-end tests through the binary

**Files:**
- Create: `tests/population.rs`

- [ ] **Step 1: Write the failing tests**

```rust
//! `kiln population`: raster → per-unit Groups, rolled up the hierarchy.

use std::path::Path;
use std::process::Command;

use assert_cmd::prelude::*;
use base64::Engine;
use httptest::{matchers::*, responders::*, Expectation, Server};
use serde_json::{json, Value};

const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/raster/pop_small_cog.tif");
const BOUNDARY_EXT: &str = "https://icr.healthcampaigns.org/StructureDefinition/location-boundary-geojson";
const SD: &str = "https://icr.healthcampaigns.org/StructureDefinition";

fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> String {
    format!(r#"{{"type":"Polygon","coordinates":[[[{x0},{y1}],[{x1},{y1}],[{x1},{y0}],[{x0},{y0}],[{x0},{y1}]]]}}"#)
}

fn admin(id: &str, name: &str, part_of: Option<&str>, boundary: Option<&str>) -> Value {
    let mut r = json!({
        "resourceType": "Location", "id": id, "name": name, "status": "active",
        "meta": {"versionId": "3"},
        "type": [{"coding": [{"system": "https://icr.healthcampaigns.org/CodeSystem/icr-location-type", "code": "admin-unit"}]}],
    });
    if let Some(p) = part_of {
        r["partOf"] = json!({"reference": format!("Location/{p}")});
    }
    if let Some(b) = boundary {
        let data = base64::engine::general_purpose::STANDARD.encode(b.as_bytes());
        r["extension"] = json!([{"url": BOUNDARY_EXT, "valueAttachment": {"contentType": "application/geo+json", "data": data}}]);
    }
    r
}

fn facility(id: &str, part_of: &str, lon: f64, lat: f64) -> Value {
    json!({"resourceType": "Location", "id": id, "name": id, "status": "active",
        "type": [{"coding": [{"code": "facility"}]}],
        "partOf": {"reference": format!("Location/{part_of}")},
        "position": {"longitude": lon, "latitude": lat}})
}

fn write_snapshot(dir: &Path, resources: &[Value]) {
    let text: String = resources.iter().map(|r| format!("{r}\n")).collect();
    std::fs::write(dir.join("locations.ndjson"), text).unwrap();
}

fn run(dir: &Path, raster: &str, extra: &[&str]) -> assert_cmd::assert::Assert {
    let out = dir.join("groups.ndjson");
    let report = dir.join("report.json");
    Command::cargo_bin("kiln")
        .unwrap()
        .args(["population", "--snapshot"])
        .arg(dir)
        .args(["--raster", raster, "--year", "2026", "--out"])
        .arg(&out)
        .arg("--report")
        .arg(&report)
        .args(extra)
        .assert()
}

fn groups(dir: &Path) -> Vec<Value> {
    std::fs::read_to_string(dir.join("groups.ndjson"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn report(dir: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(dir.join("report.json")).unwrap()).unwrap()
}

fn ext<'a>(g: &'a Value, name: &str) -> &'a Value {
    let url = format!("{SD}/{name}");
    g["extension"].as_array().unwrap().iter().find(|e| e["url"] == url).unwrap_or(&Value::Null)
}

/// Country covering the whole fixture raster, two states splitting it at lon 5.
fn two_state_country(east_boundary: Option<&str>) -> Vec<Value> {
    vec![
        admin("ng", "Nigeria", None, Some(&rect(3.0, 4.0, 7.0, 7.0))),
        admin("west", "West State", Some("ng"), Some(&rect(3.0, 4.0, 5.0, 7.0))),
        admin("east", "East State", Some("ng"), east_boundary),
        facility("clinic", "west", 3.5, 6.5),
    ]
}

#[test]
fn measures_the_requested_level_and_rolls_up_to_the_country() {
    let dir = tempfile::tempdir().unwrap();
    write_snapshot(dir.path(), &two_state_country(Some(&rect(5.0, 4.0, 7.0, 7.0))));
    let assert = run(dir.path(), FIXTURE, &["--level", "1", "--planning-denominator"]).success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(stdout.contains("Wrote 3 Groups"), "{stdout}");
    assert!(stdout.contains("2 measured at level 1, 1 rolled up"), "{stdout}");
    assert!(stdout.contains("Raster total 1761153; assigned to level-1 units 1761153 (100.0%)"), "{stdout}");

    let gs = groups(dir.path());
    assert_eq!(gs.len(), 3);
    let by_id = |id: &str| gs.iter().find(|g| g["id"] == id).unwrap_or_else(|| panic!("no {id}"));

    let west = by_id("pop-worldpop-2026-west");
    assert_eq!(west["resourceType"], "Group");
    assert_eq!(west["meta"]["profile"][0], format!("{SD}/ICRTargetPopulation"));
    assert_eq!(west["quantity"], 875_793);
    assert_eq!(west["characteristic"][0]["valueReference"]["reference"], "Location/west");
    assert_eq!(west["characteristic"][0]["valueReference"]["display"], "West State");
    assert_eq!(ext(west, "is-calculated")["valueBoolean"], false);
    assert_eq!(ext(west, "is-planning-denominator")["valueBoolean"], true);
    assert_eq!(ext(west, "estimate-date")["valueDate"], "2026-01-01");
    let src = &ext(west, "denominator-source")["valueCodeableConcept"];
    assert_eq!(src["coding"][0]["code"], "worldpop");
    assert!(src["text"].as_str().unwrap().contains("pop_small_cog.tif"), "{src}");

    assert_eq!(by_id("pop-worldpop-2026-east")["quantity"], 885_360);

    let ng = by_id("pop-worldpop-2026-ng");
    assert_eq!(ng["quantity"], 1_761_153, "country = west + east exactly");
    assert_eq!(ext(ng, "is-calculated")["valueBoolean"], true);

    assert!(gs.iter().all(|g| g["resourceType"] == "Group"), "Locations are never written");
    let rep = report(dir.path());
    assert!(rep["counts"].get("no_boundary").is_none(), "{rep}");
    assert!(rep["counts"].get("rollup_incomplete").is_none(), "{rep}");
}

#[test]
fn a_unit_without_a_boundary_is_reported_and_its_ancestors_are_flagged() {
    let dir = tempfile::tempdir().unwrap();
    write_snapshot(dir.path(), &two_state_country(None));
    let assert = run(dir.path(), FIXTURE, &["--level", "1"]).success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(stdout.contains("Wrote 2 Groups"), "{stdout}");
    assert!(stdout.contains("(49.7%)"), "{stdout}");

    let gs = groups(dir.path());
    assert_eq!(gs.len(), 2);
    assert!(gs.iter().all(|g| g["id"] != "pop-worldpop-2026-east"));
    let ng = gs.iter().find(|g| g["id"] == "pop-worldpop-2026-ng").unwrap();
    assert_eq!(ng["quantity"], 875_793, "under-counts, and the report says so");
    assert!(gs.iter().all(|g| ext(g, "is-planning-denominator").is_null()), "flag off by default");

    let rep = report(dir.path());
    assert_eq!(rep["counts"]["no_boundary"], 1);
    assert_eq!(rep["counts"]["rollup_incomplete"], 1);
    assert_eq!(rep["issues"].as_array().unwrap().iter().find(|i| i["kind"] == "no_boundary").unwrap()["location_id"], "east");
}

#[test]
fn measuring_the_country_itself_needs_no_rollup_and_reruns_are_identical() {
    let dir = tempfile::tempdir().unwrap();
    write_snapshot(dir.path(), &two_state_country(Some(&rect(5.0, 4.0, 7.0, 7.0))));
    run(dir.path(), FIXTURE, &["--level", "0"]).success();
    let first = std::fs::read_to_string(dir.path().join("groups.ndjson")).unwrap();
    let gs = groups(dir.path());
    assert_eq!(gs.len(), 1);
    assert_eq!(gs[0]["id"], "pop-worldpop-2026-ng");
    assert_eq!(gs[0]["quantity"], 1_761_153);
    assert_eq!(ext(&gs[0], "is-calculated")["valueBoolean"], false);

    run(dir.path(), FIXTURE, &["--level", "0"]).success();
    assert_eq!(std::fs::read_to_string(dir.path().join("groups.ndjson")).unwrap(), first);
}

#[test]
fn no_units_at_the_level_and_a_bad_source_are_usage_errors() {
    let dir = tempfile::tempdir().unwrap();
    write_snapshot(dir.path(), &two_state_country(None));
    run(dir.path(), FIXTURE, &["--level", "3"])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("no admin units at level 3"));
    run(dir.path(), FIXTURE, &["--level", "1", "--source", "world pop"])
        .failure()
        .code(2)
        .stderr(predicates::str::contains("--source"));
    assert!(!dir.path().join("groups.ndjson").exists());
}

#[test]
fn a_url_raster_is_fetched_once_into_the_snapshot_cache() {
    let server = Server::run();
    server.expect(
        Expectation::matching(request::method_path("GET", "/nga_pop_2026_CN_100m_cog.tif"))
            .times(1)
            .respond_with(status_code(200).body(std::fs::read(FIXTURE).unwrap())),
    );
    let url = server.url("/nga_pop_2026_CN_100m_cog.tif").to_string();
    let dir = tempfile::tempdir().unwrap();
    write_snapshot(dir.path(), &two_state_country(Some(&rect(5.0, 4.0, 7.0, 7.0))));

    let first = run(dir.path(), &url, &["--level", "1"]).success();
    let stdout = String::from_utf8_lossy(&first.get_output().stdout).into_owned();
    assert!(stdout.contains("Raster nga_pop_2026_CN_100m_cog.tif"), "{stdout}");
    assert!(!stdout.contains("from cache"), "{stdout}");
    assert_eq!(std::fs::read_dir(dir.path().join("rasters")).unwrap().count(), 2);

    let second = run(dir.path(), &url, &["--level", "1"]).success();
    let stdout = String::from_utf8_lossy(&second.get_output().stdout).into_owned();
    assert!(stdout.contains("from cache"), "{stdout}");
    assert_eq!(groups(dir.path()).len(), 3);
}
```

- [ ] **Step 2: Run the integration tests**

Run: `cargo test --test population 2>&1 | tail -12`
Expected: `5 passed`. The `49.7%` figure is `875793 / 1761153 = 49.73%`.

- [ ] **Step 3: Run everything as CI does**

```bash
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test --locked 2>&1 | grep -E "^test result" 
```
Expected: every `test result: ok`; unit count is 231 + the new tests (2 mod, 4 geotiff, 8 zonal, 5 fetch, 2 group, 2 population = 23) = 254.

- [ ] **Step 4: Commit**

```bash
git add tests/population.rs
git commit -m "population: end-to-end tests (rollup, missing boundary, level 0, usage errors, URL cache)"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` — Commands block (~line 176), after `### index` (~line 661), report kinds (~line 462), "No GDAL" design decision (~line 810), "What kiln does not do" (~line 826), "Future extensions → Other resource types" (~line 866), Repository layout (~line 930)

- [ ] **Step 1: Commands block**

Add after the `kiln index` lines inside the fenced block:

```
kiln population --snapshot DIR --raster PATH_OR_URL --level N --year YYYY
               --out GROUPS.ndjson [--source worldpop] [--planning-denominator]
               [--report FILE] [--cache-dir DIR] [--retries 3] [--timeout SECS]
```

- [ ] **Step 2: A `### population` section, placed after `### index` and before `### load`**

```markdown
### population

```
kiln population --snapshot DIR --raster PATH_OR_URL --level N --year YYYY --out GROUPS.ndjson
                [--source worldpop] [--planning-denominator] [--report FILE] [--cache-dir DIR]
```

A campaign needs a denominator for every admin unit, and the ICR IG says
where it lives: not on the Location, but beside it as an
**`ICRTargetPopulation` Group** — a conceptual cohort (`actual = false`)
with a head count, scoped to the Location through its `geography`
characteristic, carrying source and date provenance in extensions
(`denominator-source`, `denominator-type`, `estimate-date`,
`is-calculated`, `is-planning-denominator`). `population` produces those
Groups from a population raster and the boundaries already in the snapshot.

`--raster` is a GeoTIFF: a local file, or an http(s) URL fetched once into
`SNAPSHOT/rasters/` (the same content-addressed cache the boundary fetch
uses). WorldPop's constrained 100 m grids are the intended input — one band
of 32-bit floats, EPSG:4326, `-99999` nodata, DEFLATE tiles; any north-up
lon/lat GeoTIFF with those sample types works. A projected raster is
refused, as is anything without `ModelPixelScale`/`ModelTiepoint`.

`--level N` picks the admin level to **measure**: every admin unit at that
level is summed against the raster under the *pixel-centroid rule* — a pixel
belongs to the unit its centre falls in. That rule makes adjacent units a
partition of the pixels, so children add up to their parent exactly;
"all touched" methods count every shared boundary pixel twice. Every admin
ancestor of a measured unit then gets a **rolled-up** total, flagged
`is-calculated = true`. Units below `--level`, facilities and other points
are ignored. The last line of output compares the raster's grand total with
the sum assigned to the measured units; the gap is population whose pixel
centres fall outside every boundary (coastline slivers, gaps between
polygons, or missing units).

Group ids are `pop-SOURCE-YEAR-LOCATIONID`, so a re-run rewrites the same
resources; `kiln load` sends them as plain `PUT`s (no `ifMatch` — kiln owns
these Groups outright). Load the Locations first: `load` orders `partOf`
parents before children but knows nothing about a Group's `geography`
reference. `--year` becomes `estimate-date` (`YYYY-01-01`), `--source` the
`denominator-source` code (`worldpop` by default; anything in
`icr-denominator-source-cs`), and `--planning-denominator` marks the written
Groups as the figures campaigns plan against.

Report kinds: `no_boundary` (a unit at `--level` had no usable polygon),
`no_pixels` (a polygon with no data pixel centre inside — a unit smaller than a
pixel, or entirely outside the raster), `rollup_incomplete` (an ancestor whose
total under-counts because a descendant was `no_boundary`), `rollup_empty`,
`group_id_too_long`. Hierarchy problems from pass one (`orphan`, `cycle`, …)
appear too, since `population` reads the snapshot with the same index
`transform` builds.

What this is not: age–sex bands (WorldPop publishes those as separate
rasters; run `population` once per raster with a different `--source` when
the IG gains a characteristic for them), coverage-weighted sums, or anything
that changes a Location.
```

- [ ] **Step 3: Report kinds list**

Add a bullet to "The kinds, grouped by where they arise":

```markdown
- Population (`kiln population`): `no_boundary`, `no_pixels`, `rollup_incomplete`,
  `rollup_empty`, `group_id_too_long`
```

- [ ] **Step 4: Design decision, non-goals, future, layout**

In **"No GDAL, no GEOS, no Python at runtime."** append: `The population raster is read the same way: the pure-Rust `tiff` crate decodes WorldPop's DEFLATE tiles, and kiln's own scanline fill does the zonal sum.`

In **"What kiln does not do → Import external data"** change the first sentence to: `- **Import external data into Locations.** Turning a GRID3 ward file or a CSV of facilities into FHIR resources is the job of `bake` and `bake-points`, … (unchanged). Then add a sentence: `` `kiln population` is the deliberate exception: it derives Groups from the registry's own boundaries and a published raster, and writes nothing onto a Location. ``

In **"Future extensions → Other resource types"** append: `` `Group` has a first foothold: `kiln population` writes `ICRTargetPopulation` Groups, though it does not yet extract or project them. ``

In **Repository layout** add under `src/`: `    raster/      GeoTIFF reader (tiff crate), pixel-centroid zonal sum, raster fetch cache` and `    population.rs` after `cells.rs`-equivalent lines (add `cells.rs` too if it is missing from the list), and under `tests/`: `    fixtures/raster/   a 40×30 COG with known sums` and `population.rs` in the test file list.

- [ ] **Step 5: Verify and commit**

Run: `cargo test --locked 2>&1 | grep -c "^test result: ok"` → the same count of suites as before plus one (`population`).

```bash
git add README.md
git commit -m "docs: kiln population — denominators as ICRTargetPopulation Groups from a WorldPop raster"
```

---

### Task 9: Smoke test against the real Nigeria raster (manual, not committed)

**Files:** none. This validates the design assumption that the COG on R2 decodes and that a national run is fast enough.

- [ ] **Step 1: Find a snapshot with Nigeria admin units**

The Nigeria registry loaded on 2026-09-06 (see `~/github/icr/tools/hapi/README.md`); extract it if no snapshot is at hand:

```bash
cargo build --release
./target/release/kiln extract --server http://localhost:8080/fhir --snapshot /tmp/nga-snap
```

- [ ] **Step 2: Run at LGA level (admin level 2) for 2026**

```bash
time ./target/release/kiln population --snapshot /tmp/nga-snap \
  --raster https://pub-e6e33253461449dfb455784209fd31bb.r2.dev/nga_pop_2026_CN_100m_cog.tif \
  --level 2 --year 2026 --planning-denominator \
  --out /tmp/nga-pop-2026.ndjson --report /tmp/nga-pop-2026-report.json
```

Expected: `Raster nga_pop_2026_CN_100m_cog.tif (166.9 MB)`; ~774 measured + 37 states + 1 country Groups; the assigned share should be in the high nineties (WorldPop's constrained grid has population only where there is settlement; coastline slivers and any LGA gaps account for the rest). A second run prints `from cache` and finishes in seconds. Record the timing and the share in the commit message of a follow-up README tweak if either is surprising.

- [ ] **Step 3: Load into the local HAPI and check one Group**

```bash
./target/release/kiln load --server http://localhost:8080/fhir --in /tmp/nga-pop-2026.ndjson --dry-run
./target/release/kiln load --server http://localhost:8080/fhir --in /tmp/nga-pop-2026.ndjson
curl -s 'http://localhost:8080/fhir/Group?_profile=https://icr.healthcampaigns.org/StructureDefinition/ICRTargetPopulation&_count=1' | head -c 800
```

If HAPI rejects the profile canonical (unknown profile), that is a HAPI validation setting, not a kiln bug — the IG's `ICRTargetPopulation` is what `~/github/icr/tools/campaign-builder` already loads.

---

## Self-review

**Spec coverage.** Requirements from the design summary → tasks: read WorldPop COGs without GDAL (T1–T2); pixel-centroid zonal sum with exact partition (T3, tested by left+right=whole); use pixel's existing COGs on R2 via URL with caching (T4, T9); emit `ICRTargetPopulation` Groups with `worldpop` source, estimate-date, is-calculated for rollups, optional planning flag (T5–T6); roll up through the hierarchy and flag incomplete parents (T6 unit test + T7); deterministic ids / idempotent re-run (T7 third test); usage errors exit 2 (T7); documentation including the `load` ordering caveat (T8). Not covered on purpose: campaign-builder swap and HAPI recipe live in the ICR repo (listed as out of scope).

**Placeholder scan.** No TBD/TODO. Every code step shows the code; every run step names the command and the expected result.

**Type consistency.** `Raster::read_tile(&mut self, tx: u32, ty: u32) -> Result<Tile>` is used identically in T1 (MemRaster), T2 (GeoTiff), T3 (zonal). `ZonalSum { sum: f64, pixels: u64 }` flows from T3 into T6's `roll_up(…, measured: Option<ZonalSum>)`. `fetch::load(source: &str, cache_dir: &Path, retries: usize, timeout: Duration) -> Result<RasterBytes { bytes, label, cached }>` matches its call in T6. `TargetPopulation` fields in T5 match the struct literal in T6. `GeoTiff::FIXTURE` is `#[cfg(test)] pub` in T2 and used by T3/T4 tests. `Snapshot::rasters()` (T4) is used in T6. `write_report(&Path, &Report)` from `transform.rs` is used as-is.
