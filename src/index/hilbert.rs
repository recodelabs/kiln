//! Hilbert curve key for spatial clustering of rows within a partition.
//! Classic xy2d on a 2^ORDER grid over the dataset extent.

pub const ORDER: u32 = 16;

/// `point` is (x, y); `extent` is [xmin, ymin, xmax, ymax] of the whole dataset.
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
        let rx = u64::from(x & s > 0);
        let ry = u64::from(y & s > 0);
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
    fn nearby_points_get_nearby_keys() {
        let extent = [0.0, 0.0, 100.0, 100.0];
        let a = hilbert_key([10.0, 10.0], extent);
        let b = hilbert_key([10.1, 10.1], extent);
        let far = hilbert_key([90.0, 90.0], extent);
        assert!(a.abs_diff(b) < a.abs_diff(far));
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
