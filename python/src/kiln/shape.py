"""Shape guard for untrusted FHIR/GeoJSON list fields.

FHIR resources arrive over the network in `extract.py`, and boundary GeoJSON
is decoded from bytes that nobody but a partner's export tool has ever
validated; `profile.py` and `geometry.py` then read that untrusted shape
before a server or schema has enforced anything. A field the spec declares
0..* (`Bundle.entry`, `Location.extension`, `CodeableConcept.coding`,
`FeatureCollection.features`, ...) can arrive as any other JSON type — most
dangerously as a bare string, which is iterable but is never the list the
cardinality promised. `for item in resource.get("extension")` then silently
walks characters instead of raising, and `x.get("field") or []` does not
save you: a truthy non-list (a dict, a nonzero int, a non-empty string)
sails straight through `or []` unchanged.

`as_list` is the one place that question gets asked. Every module in this
package iterates or indexes a nominally-0..*-cardinality field through it
instead of re-deriving the same `isinstance` check at each call site — that
duplication is exactly what let three review rounds each close a named
crash and have the next round find the same class one level up the call
stack.
"""

from __future__ import annotations


def as_list(value: object) -> list:
    """Return `value` if it's a list, else `[]`.

    Use this everywhere a FHIR/GeoJSON field is documented as 0..* before
    iterating or indexing it — including in place of the `x or []` idiom,
    which only guards against `None`/falsy values, not against a wrong,
    truthy type (a dict, a number, or — worst of all — a string, which is
    iterable and so fails silently rather than raising).
    """
    return value if isinstance(value, list) else []
