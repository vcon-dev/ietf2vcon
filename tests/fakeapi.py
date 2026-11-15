"""Pieces shared by the fake Datatracker APIs in the tests.

`listing()` returns a list response in the real shape: `meta` carries `limit`,
`next`, `offset`, `previous` and `total_count`, whatever the resource.
"""

import random


def listing(path, objects, params, page_cap=None, shuffle=False, ignore_offset=False):
    """A paged list response.

    shuffle: rows come back in a different order for each offset unless
        `order_by` is passed, as the real history resources can.
    ignore_offset: the server answers every request with the first page.
    page_cap: the most rows the server returns per page.
    """
    rows = list(objects)
    order_by = params.get("order_by")
    if order_by:
        descending = order_by.startswith("-")
        rows.sort(key=lambda r: int(r["id"]), reverse=descending)
    elif shuffle:
        random.Random(int(params.get("offset", 0)) + 1).shuffle(rows)
    limit = int(params.get("limit", 20))
    if page_cap:
        limit = min(limit, page_cap)
    offset = 0 if ignore_offset else int(params.get("offset", 0))
    page = rows[offset:offset + limit]
    more = offset + limit < len(rows)
    return {
        "meta": {
            "limit": limit,
            "next": f"{path}?limit={limit}&offset={offset + limit}" if more else None,
            "offset": offset,
            "previous": f"{path}?limit={limit}&offset={max(offset - limit, 0)}" if offset else None,
            "total_count": len(rows),
        },
        "objects": page,
    }
