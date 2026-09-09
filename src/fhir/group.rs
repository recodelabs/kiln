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
            // denominator-type is a bare code in the IG (value[x] only code), unlike denominator-source.
            {"url": DENOMINATOR_TYPE_EXTENSION_URL, "valueCode": "total-population"},
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
            source_text:
                "nga_pop_2026_CN_100m_cog.tif; pixel-centroid zonal sum over admin level 2",
            calculated: false,
            planning_denominator: planning,
        })
    }

    fn ext<'a>(g: &'a Value, url: &str) -> Option<&'a Value> {
        g["extension"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["url"] == url)
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
        assert_eq!(
            g["name"],
            "Total population, Kano Municipal, 2026 (WorldPop modelled estimate)"
        );
        let c = &g["characteristic"][0];
        assert_eq!(
            c["code"]["coding"][0]["system"],
            GROUP_CHARACTERISTIC_SYSTEM
        );
        assert_eq!(c["code"]["coding"][0]["code"], "geography");
        assert_eq!(c["valueReference"]["reference"], "Location/lga-1");
        assert_eq!(c["valueReference"]["display"], "Kano Municipal");
        assert_eq!(c["exclude"], false);
        let src = ext(&g, DENOMINATOR_SOURCE_EXTENSION_URL).unwrap();
        assert_eq!(
            src["valueCodeableConcept"]["coding"][0]["system"],
            DENOMINATOR_SOURCE_SYSTEM
        );
        assert_eq!(src["valueCodeableConcept"]["coding"][0]["code"], "worldpop");
        assert_eq!(
            src["valueCodeableConcept"]["coding"][0]["display"],
            "WorldPop modelled estimate"
        );
        assert!(src["valueCodeableConcept"]["text"]
            .as_str()
            .unwrap()
            .contains("nga_pop_2026"));
        assert_eq!(
            ext(&g, DENOMINATOR_TYPE_EXTENSION_URL).unwrap()["valueCode"],
            "total-population"
        );
        assert_eq!(
            ext(&g, ESTIMATE_DATE_EXTENSION_URL).unwrap()["valueDate"],
            "2026-01-01"
        );
        assert_eq!(
            ext(&g, IS_CALCULATED_EXTENSION_URL).unwrap()["valueBoolean"],
            false
        );
        assert!(ext(&g, IS_PLANNING_DENOMINATOR_EXTENSION_URL).is_none());
    }

    #[test]
    fn planning_flag_adds_the_extension_and_unknown_sources_have_no_display() {
        let g = sample(true);
        assert_eq!(
            ext(&g, IS_PLANNING_DENOMINATOR_EXTENSION_URL).unwrap()["valueBoolean"],
            true
        );

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
        let coding = &ext(&g, DENOMINATOR_SOURCE_EXTENSION_URL).unwrap()["valueCodeableConcept"]
            ["coding"][0];
        assert!(coding.get("display").is_none());
        assert_eq!(
            ext(&g, IS_CALCULATED_EXTENSION_URL).unwrap()["valueBoolean"],
            true
        );
    }

    #[test]
    fn the_id_is_deterministic_and_within_the_fhir_limit_for_typical_ids() {
        assert_eq!(
            group_id("worldpop", 2026, "a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
            "pop-worldpop-2026-a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        );
        assert!(
            group_id("worldpop", 2026, "a1b2c3d4-e5f6-7890-abcd-ef1234567890").len() <= MAX_ID_LEN
        );
        assert_eq!(group_id("x", 2026, "l"), group_id("x", 2026, "l"));
    }
}
