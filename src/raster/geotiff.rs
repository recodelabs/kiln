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
/// GeoKey 1025 (GTRasterType): 1 = PixelIsArea (what `GeoTransform` models),
/// 2 = PixelIsPoint (coordinates name pixel centres; would need a half-pixel shift).
const GT_RASTER_TYPE_KEY: u16 = 1025;
const RASTER_TYPE_PIXEL_IS_POINT: u16 = 2;

#[derive(Debug)]
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
        check_geokeys(&mut decoder, label)?;
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
        if tile.0 == 0 || tile.1 == 0 || width == 0 || height == 0 {
            return Err(KilnError::Usage(format!(
                "{label}: empty raster or tile ({width}×{height}, tiles {}×{})",
                tile.0, tile.1
            )));
        }
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
    GeoTransform::new(tie[3] - tie[0] * px, tie[4] + tie[1] * py, px, py)
        .map_err(|e| KilnError::Usage(format!("{label}: {e}")))
}

/// Refuse what `GeoTransform` cannot express: a projected raster (its pixel
/// coordinates would be metres while the boundaries are lon/lat) and a
/// PixelIsPoint raster (coordinates name pixel centres, not corners). A file
/// without a GeoKeyDirectory is allowed through — nothing to check.
fn check_geokeys(decoder: &mut Decoder<Cursor<Vec<u8>>>, label: &str) -> Result<()> {
    let Some(keys) = decoder
        .find_tag_unsigned_vec::<u16>(Tag::GeoKeyDirectoryTag)
        .map_err(|e| tiff_err(label, e))?
    else {
        return Ok(());
    };
    // Header is 4 shorts, then one (key, location, count, value) per entry;
    // location 0 means the value is inline.
    for entry in keys.as_chunks::<4>().0.iter().skip(1) {
        let (key, location, value) = (entry[0], entry[1], entry[3]);
        if location != 0 {
            continue;
        }
        if key == GT_MODEL_TYPE_KEY && value != MODEL_TYPE_GEOGRAPHIC {
            return Err(KilnError::Usage(format!(
                "{label}: not a geographic (lon/lat) raster (GTModelType = {value}); reproject to EPSG:4326 first"
            )));
        }
        if key == GT_RASTER_TYPE_KEY && value == RASTER_TYPE_PIXEL_IS_POINT {
            return Err(KilnError::Usage(format!(
                "{label}: PixelIsPoint rasters are not supported (coordinates name pixel centres); rewrite as PixelIsArea"
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
    fn transform(&self) -> GeoTransform {
        self.transform
    }
    fn nodata(&self) -> Option<f32> {
        self.nodata
    }
    fn tile_size(&self) -> (u32, u32) {
        self.tile
    }
    fn read_tile(&mut self, tx: u32, ty: u32) -> Result<Tile> {
        let across = self.tiles_across();
        let down = self.tiles_down();
        if tx >= across || ty >= down {
            return Err(KilnError::Usage(format!(
                "{}: tile ({tx}, {ty}) is outside the {across}×{down} grid",
                self.label
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
                        "{}: tile {index} decoded to {} values, expected {width}×{height}",
                        self.label,
                        data.len()
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
    use crate::raster::MemRaster;

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
        let err = g.read_tile(3, 0).unwrap_err();
        assert_eq!(err.exit_code(), 2);
        assert!(g.read_tile(0, 2).is_err());
    }

    #[test]
    fn every_tile_matches_the_in_memory_raster_and_sums_to_the_known_total() {
        let mut g = fixture();
        let mut m = MemRaster::fixture();
        let (mut sum, mut n) = (0.0f64, 0u64);
        for ty in 0..g.tiles_down() {
            for tx in 0..g.tiles_across() {
                let t = g.read_tile(tx, ty).unwrap();
                assert_eq!(t, m.read_tile(tx, ty).unwrap(), "tile ({tx}, {ty})");
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
