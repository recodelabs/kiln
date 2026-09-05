//! Hilbert curve key for spatial clustering of rows within a partition.
//! Classic xy2d on a 2^ORDER grid over the caller-supplied extent.

pub const ORDER: u32 = 16;

/// `point` is (x, y); `extent` is the [xmin, ymin, xmax, ymax] the caller
/// wants `point` scaled against. This function is extent-agnostic: it has
/// no notion of a dataset's own bounds. `build_index` (see `HILBERT_EXTENT`
/// in `src/index/build.rs`) always passes a fixed WGS84 extent so that
/// keys are comparable across partitions and runs, rather than a
/// per-dataset bounding box.
///
/// Coordinates are expected to be finite; a NaN coordinate maps to the
/// origin rather than panicking. The extent's endpoints map exactly onto
/// grid cells `0` and `n - 1`, so those two edge cells are deliberately
/// half the width of the interior cells. Resolution scales with the
/// extent: at `ORDER = 16` a 10-degree-wide extent gives cells of about
/// 17 m on a side.
pub fn hilbert_key(point: [f64; 2], extent: [f64; 4]) -> u64 {
    let n = 1u64 << ORDER;
    let scale = |v: f64, lo: f64, hi: f64| -> u64 {
        if hi <= lo {
            return 0;
        }
        let t = ((v - lo) / (hi - lo)).clamp(0.0, 1.0);
        ((t * (n - 1) as f64).round() as u64).min(n - 1)
    };
    let mut x = scale(point[0], extent[0], extent[2]);
    let mut y = scale(point[1], extent[1], extent[3]);
    let mut d = 0u64;
    let mut s = n >> 1;
    while s > 0 {
        let rx = u64::from((x & s) > 0);
        let ry = u64::from((y & s) > 0);
        d += s * s * ((3 * rx) ^ ry);
        // rotate
        if ry == 0 {
            if rx == 1 {
                x = n - 1 - x;
                y = n - 1 - y;
            }
            std::mem::swap(&mut x, &mut y);
        }
        s >>= 1;
    }
    d
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corners_map_to_curve_ends() {
        let extent = [0.0, 0.0, 10.0, 10.0];
        assert_eq!(hilbert_key([0.0, 0.0], extent), 0);
        let last = hilbert_key([10.0, 0.0], extent);
        assert_eq!(last, (1u64 << (2 * ORDER)) - 1);
    }

    #[test]
    fn hilbert_order_reduces_total_travel_distance() {
        let extent = [0.0, 0.0, 1.0, 1.0];
        let count = 10_000;

        // Simple LCG (numerical-recipes constants); no new deps needed.
        let mut state: u64 = 0x2545_f491_4f6c_dd1d;
        let mut next_unit = || {
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            ((state >> 33) as f64) / (1u64 << 31) as f64
        };

        let mut points: Vec<[f64; 2]> = (0..count).map(|_| [next_unit(), next_unit()]).collect();

        let dist = |a: [f64; 2], b: [f64; 2]| {
            let dx = a[0] - b[0];
            let dy = a[1] - b[1];
            (dx * dx + dy * dy).sqrt()
        };
        let travel_distance =
            |pts: &[[f64; 2]]| -> f64 { pts.windows(2).map(|w| dist(w[0], w[1])).sum() };

        let unsorted_sum = travel_distance(&points);

        points.sort_by_key(|p| hilbert_key(*p, extent));
        let sorted_sum = travel_distance(&points);

        // Measured ratio is about 53x; require at least 10x to leave headroom.
        assert!(sorted_sum < unsorted_sum / 10.0);
    }

    #[test]
    fn degenerate_extent_does_not_panic() {
        assert_eq!(hilbert_key([5.0, 5.0], [5.0, 5.0, 5.0, 5.0]), 0);
    }

    #[test]
    fn keys_are_unique_across_a_grid() {
        let extent = [0.0, 0.0, 1.0, 1.0];
        let mut keys: Vec<u64> = Vec::new();
        for i in 0..64 {
            for j in 0..64 {
                keys.push(hilbert_key([i as f64 / 63.0, j as f64 / 63.0], extent));
            }
        }
        keys.sort_unstable();
        keys.dedup();
        assert_eq!(keys.len(), 64 * 64);
    }
}
