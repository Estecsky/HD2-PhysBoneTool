"""Conservative, non-mutating physics reachability; never deletes source bones."""
from __future__ import annotations


def prune(project, parents, consumers=None, available=None):
    """Accept converter plans or canonical snapshots. None means unknown consumers.

    A broken chain is omitted whole: stitching across a missing joint would invent
    motion. Collision/strip dependencies are undirected conservatively; ancestry
    dependencies retain the chain supplying a surviving child's driver.
    """
    result = dict(project)
    canonical = 'profiles' in project
    nodes_key, bone_key = ('nodes', 'bone_name') if canonical else ('joints', 'bone')
    id_key = 'uuid' if canonical else 'name'
    link_keys = ('chain_a_uuid', 'chain_b_uuid') if canonical else ('chain_a', 'chain_b')
    available = set(parents) if available is None else set(available)
    removed = {'chains': [], 'colliders': [], 'chain_links': [], 'profiles': []}

    def ancestors(names):
        found = set()
        for name in names:
            seen = set()
            while name:
                if name in seen:
                    raise ValueError('物理依赖骨架存在父链循环：' + name)
                seen.add(name)
                found.add(name)
                name = parents.get(name)
        return found

    def omit(kind, row, reason):
        removed[kind].append({'name': row.get('display_name', row.get('name', row.get('uuid', ''))),
                              'reason': reason})

    chains = []
    for chain in project['chains']:
        names = [n[bone_key] for n in chain[nodes_key]]
        if any(name not in available for name in names):
            omit('chains', chain, 'missing_bone_or_mapping')
        else:
            chains.append(chain)
    identities = [c[id_key] for c in chains]
    if len(set(identities)) != len(identities):
        raise ValueError('物理链标识重复，不能安全清理')
    by_id = dict(zip(identities, chains))

    def filters(row):
        values = [(row['group_mask'], row['collision_mask'])]
        for node in row.get(nodes_key, ()):
            if node.get('override_collision_filter'):
                values.append((node['group_mask'], node['collision_mask']))
        return values

    def collides(a, b):
        return any(ga & mb and gb & ma for ga, ma in filters(a) for gb, mb in filters(b))

    dependencies = {name: {name} for name in by_id}
    chain_bones = {c[id_key]: {n[bone_key] for n in c[nodes_key]} for c in chains}
    outputs = {c[id_key]: {n[bone_key] for n in c[nodes_key][0 if c.get('root_swing') else 1:]}
               for c in chains}
    for first in chains:
        a = first[id_key]
        ancestry = ancestors(chain_bones[a])
        for second in chains:
            b = second[id_key]
            if outputs[b] & ancestry or collides(first, second):
                dependencies[a].add(b)
    links = []
    for link in project.get('chain_links', ()):
        a, b = (link[k] for k in link_keys)
        if a not in by_id or b not in by_id:
            omit('chain_links', link, 'missing_chain')
        else:
            dependencies[a].add(b)
            dependencies[b].add(a)
            links.append(link)
    colliders = []
    simulated = set().union(*outputs.values()) if outputs else set()
    for collider in project['colliders']:
        bound = {collider.get(bone_key), collider.get('bone_name_b', collider.get('bone_b'))} - {None, ''}
        if not bound <= available:
            omit('colliders', collider, 'missing_bone_or_mapping')
            continue
        dynamic = collider.get('attachment_mode') == 'DYNAMIC' or collider.get('dynamic', False)
        if dynamic and collider.get(bone_key) not in simulated:
            omit('colliders', collider, 'missing_simulated_attachment')
            continue
        users = {c[id_key] for c in chains if collides(c, collider)}
        if not users:
            omit('colliders', collider, 'no_collision_consumer')
            continue
        ancestry = ancestors(bound)
        drivers = {name for name, bones in outputs.items() if bones & ancestry}
        for user in users:
            dependencies[user].update(drivers)
        colliders.append((collider, users))
    live_bones = ancestors(consumers) if consumers is not None else None
    keep = {name for name, bones in outputs.items() if live_bones is None or bones & live_bones}
    pending = list(keep)
    while pending:
        for name in dependencies[pending.pop()] - keep:
            keep.add(name)
            pending.append(name)
    result['chains'] = [c for c in chains if c[id_key] in keep]
    for chain in chains:
        if chain[id_key] not in keep:
            omit('chains', chain, 'no_observable_consumer_or_dependency')
    result['colliders'] = []
    for collider, users in colliders:
        if users & keep:
            result['colliders'].append(collider)
        else:
            omit('colliders', collider, 'no_surviving_collision_consumer')
    result['chain_links'] = [link for link in links if link[link_keys[0]] in keep and link[link_keys[1]] in keep]
    for link in links:
        if link not in result['chain_links']:
            omit('chain_links', link, 'no_surviving_chain')
    if canonical:
        profiles = {c['spring_profile_uuid'] for c in result['chains']}
        result['profiles'] = [p for p in project['profiles'] if p['uuid'] in profiles]
        for profile in project['profiles']:
            if profile['uuid'] not in profiles:
                omit('profiles', profile, 'unused_profile')
        result['new_chain_profile_uuid'] = next(iter(sorted(profiles)), '')
    else:
        needed = {n[bone_key] for c in result['chains'] for n in c[nodes_key]}
        result['helpers'] = [h for h in project.get('helpers', ()) if h['name'] in needed]
    report = {'removed': removed, 'counts': {key: len(rows) for key, rows in removed.items()},
              'consumer_analysis': 'unknown_preserve' if consumers is None else 'dependency_checked',
              'source_bones_deleted': 0}
    return result, report


def prune_conversion(plan, snapshot):
    parents = {b['name']: b.get('parent') for b in snapshot['bones'] + plan.get('helpers', [])}
    result, report = prune(plan, parents, snapshot.get('consumer_bones'))
    result['report'] = dict(plan['report'], pruning=report)
    counts = dict(result['report']['counts'])
    counts.update(chains=len(result['chains']), colliders=len(result['colliders']),
                  helpers=len(result['helpers']), strip_links=len(result['chain_links']))
    result['report']['counts'] = counts
    return result
