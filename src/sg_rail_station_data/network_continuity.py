"""Check ordered GTFS stop pairs against actual nodes in retained OSM passenger relations."""
from collections import defaultdict
from math import cos, hypot, radians

from .storage import read_json

TAMPINES_EWL = {
    ('EWL', 7124682660, '11885e12578782ee729e628944f2e2edb937f83ed7d1323a9dd6a4df6b3cb4c5'),
    ('EWL', 7124682661, 'b119df62cbe6cd28b9fa098035ddd5179a8d6d6630e43e7222929286c58f8d08'),
}


def _connected(graph, start, end, stops):
    # Never bridge a gap by distance or take the long way around a loop via
    # another served stop. Crossing lines must share an actual OSM node ID.
    seen = {start}
    pending = [start]
    while pending:
        node = pending.pop()
        for other in graph.get(node, ()):
            if other == end and end != start:
                return True
            if other not in stops and other not in seen:
                seen.add(other)
                pending.append(other)
    return False


def check_continuity(network, manifest, source_dir):
    stations = {s['stop_id']: s for s in network['stations']}
    wanted = defaultdict(set)
    for p in network['patterns']:
        ids = p['station_stop_ids']
        for a, b in zip(ids, ids[1:]):
            wanted[p['line_id'], a, b].add(p['id'])
    matches = defaultdict(list)
    for response in manifest['responses']:
        data = read_json(source_dir / response['file'])
        elements = {(v['type'], v['id']): v for v in data['elements']}
        relation = elements['relation', response['relation_id']]
        line = response['line_id']
        stops = []
        graph = defaultdict(set)
        for m in relation['members']:
            if m['type'] == 'node' and m.get('role') in ('stop', 'stop_entry_only', 'stop_exit_only'):
                stops.append(m['ref'])
            if m['type'] != 'way' or m.get('role', '') not in ('', 'forward', 'backward'):
                continue
            way = elements['way', m['ref']]
            tags = way['tags']
            if tags.get('service') in ('yard', 'siding', 'spur', 'crossover', 'depot') or tags.get('access') == 'private':
                continue
            # The claim is physical continuity, not signalling or permission
            # to run. Ordered relation stop members supply the service direction.
            for a, b in zip(way['nodes'], way['nodes'][1:]):
                graph[a].add(b)
                graph[b].add(a)
        mapped = []
        for node in stops:
            v = elements['node', node]
            name = v.get('tags', {}).get('name:en') or v.get('tags', {}).get('name', '')
            name = name.partition(' - ')[0].casefold()
            # The retained GTFS parent DT32 is at the Downtown Line side of
            # Tampines. Use the two exact EWL stop nodes, not a wider radius
            # for every interchange. Invalidate on any source revision.
            tampines = (network['gtfs_zip_sha256'] == '7a4f56076869e1c71d4019d34b23cec90fe0137d1a729967c66bfc8c18793d7f'
                        and (line, node, response['sha256']) in TAMPINES_EWL)
            candidates = [s['stop_id'] for s in stations.values() if line in s['line_ids']
                          and s['name'].casefold() == name and
                          ((tampines and s['stop_id'] == 'DT32' and 'EW2' in s['codes']) or
                           111195 * hypot((s['coordinates'][0] - v['lon']) * cos(radians(v['lat'])),
                                         s['coordinates'][1] - v['lat']) <= 250)]
            mapped.append(candidates[0] if len(candidates) == 1 else None)
        stop_set = set(stops)
        links = [_connected(graph, a, b, stop_set) for a, b in zip(stops, stops[1:])]
        for i, a in enumerate(mapped):
            for j in range(i + 1, len(mapped)):
                if not links[j - 1] or mapped[j] is None:
                    break
                # Prove physical continuity even for a skip-stop pattern. Do
                # not infer a service direction from an undirected rail graph.
                for start, end, direction in ((a, mapped[j], 'ordered'), (mapped[j], a, 'reverse')):
                    if (line, start, end) in wanted:
                        matches[line, start, end].append(dict(relation_id=relation['id'],
                            direction=direction, stop_hops=j-i, via=mapped[i+1:j] if direction=='ordered' else mapped[i+1:j][::-1]))
    rows = [dict(line_id=k[0], from_stop=k[1], to_stop=k[2], pattern_ids=sorted(patterns),
                 evidence=sorted(matches[k], key=lambda m: (m['stop_hops'], m['direction'], m['relation_id'])), status='connected' if matches[k] else 'review_required')
            for k, patterns in sorted(wanted.items())]
    return dict(schema_version=1, kind='physical_station_pair_continuity', pair_count=len(rows),
                review_count=sum(not row['evidence'] for row in rows), pairs=rows,
                publication_allowed=False)
