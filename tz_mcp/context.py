def references(value):
    if isinstance(value, dict):
        if '$ref' in value:
            if set(value) != {'$ref'} or not isinstance(value['$ref'], str):
                raise ValueError('A reference must contain only a string $ref')
            yield value['$ref']
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def resolve(value, context):
    if isinstance(value, dict):
        if '$ref' in value:
            list(references(value))
            current = context
            try:
                for part in value['$ref'].split('.'):
                    current = current[int(part)] if isinstance(current, list) else current[part]
                return current
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise ValueError(f"Unresolved reference: {value['$ref']}") from exc
        return {k: resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context) for v in value]
    return value
