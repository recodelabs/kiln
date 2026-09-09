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
