"""Offline derived-text review. Never creates provider or ROM evidence.

Review is a separate operator action using a separately provisioned private key.
Local HMAC seals protect integrity, not against an operator controlling the code.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
import identity as i

REVIEW_KEY_ENV = 'TUBELIU_TRANSLATION_REVIEW_KEY_FILE'


def digest(value):
    return hashlib.sha256(i._canonical(value)).hexdigest()


def binding(value):
    value = i._object(value, 'Translation target binding')
    if value.get('kind') == 'local' and set(value) == {'kind', 'system_rom_root'}:
        root = value['system_rom_root']
        if isinstance(root, str) and root == str(Path(root).resolve()):
            return copy.deepcopy(value)
    if value.get('kind') == 'android' and set(value) == {'kind', 'serial', 'remote_rom_root'}:
        if isinstance(value['serial'], str) and value['serial'].strip() and isinstance(value['remote_rom_root'], str):
            root = value['remote_rom_root']
            if root.startswith('/') and root != '/' and not any(p in ('', '.', '..') for p in root[1:].split('/')):
                return copy.deepcopy(value)
    i._fail('An exact local root or Android serial/root is required', 'translation_target_mismatch')


def target_binding(target, system):
    if target['type'] == 'local':
        return binding({'kind': 'local', 'system_rom_root': str((Path(target['rom_root']) / system).resolve())})
    return binding({'kind': 'android', 'serial': target['serial'], 'remote_rom_root': target['rom_root']})


def review_key(identity_key, explicit=None):
    path = explicit or os.environ.get(REVIEW_KEY_ENV)
    if not path:
        i._fail('A separately trusted translation reviewer key is required', 'missing_review_key')
    if i._key(path) == i._key(identity_key):
        i._fail('Reviewer and identity keys must be distinct', 'invalid_review_key')
    return path


def validate_draft(draft, candidate, *, source, system, file, fingerprint, matched):
    expected = {'schema_version', 'status', 'classification', 'source_sha256', 'system', 'file',
                'rom_fingerprint', 'matched_rom', 'provider_game_id', 'binding', 'fields', 'generator'}
    if not isinstance(draft, dict) or set(draft) != expected or draft['schema_version'] != 1 or draft['status'] != 'pending_review' or draft['classification'] != 'translation_derivative':
        i._fail('Only a pending translation derivative can be reviewed', 'invalid_translation')
    for key, value in {'source_sha256': digest(source), 'system': system, 'file': file,
                       'rom_fingerprint': fingerprint, 'matched_rom': matched,
                       'provider_game_id': str(candidate['provider_game_id'])}.items():
        if draft[key] != value:
            i._fail('Translation is bound to different source/ROM evidence', 'translation_source_mismatch')
    binding(draft['binding'])
    generator = draft['generator']
    if not isinstance(generator, dict) or set(generator) != {'model', 'prompt_sha256'} or not isinstance(generator['model'], str) or not generator['model'].strip() or not isinstance(generator['prompt_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', generator['prompt_sha256']):
        i._fail('Translation model and prompt digest are required', 'invalid_translation')
    fields = draft['fields']
    if not isinstance(fields, dict) or not fields or set(fields) - {'name', 'desc'}:
        i._fail('Only name and description translations are supported', 'invalid_translation')
    for field, item in fields.items():
        if not isinstance(item, dict) or set(item) != {'source_field', 'original', 'text', 'language'}:
            i._fail('Translation fields require retained exact originals', 'invalid_translation')
        allowed = {'name', 'original_name'} if field == 'name' else {'desc', 'description_zh', 'original_description'}
        source_field = item['source_field']
        original = candidate.get(source_field) if isinstance(source_field, str) and source_field in allowed else None
        if not isinstance(original, str) or not original.strip() or item['original'] != original:
            i._fail('Translation original is absent from the sealed source', 'translation_source_mismatch')
        text = item['text']
        if item['language'] != 'zh-CN' or not isinstance(text, str) or not text.strip() or len(text) > (512 if field == 'name' else 20000) or not re.search('[\u3400-\u9fff]', text) or any(ord(c) < 32 and c not in '\n\t' for c in text) or any(0xd800 <= ord(c) <= 0xdfff for c in text):
            i._fail('Invalid Chinese translation text', 'invalid_translation')
    return {field: item['text'] for field, item in fields.items()}


def draft(base_receipt, fields, generator, target, key_path=None):
    p = i.verify_payload(base_receipt, 'game_identity', key_path)
    i.verify_receipt(base_receipt, system=p['system'], file=p['file'], rom_fingerprint=p['rom_fingerprint'], metadata=p['metadata'], media=p['media'], key_path=key_path)
    _, candidate, matched = i._matched_candidate(p['source'], p['system'], p['rom_fingerprint'], key_path)
    value = {'schema_version': 1, 'status': 'pending_review', 'classification': 'translation_derivative',
             'source_sha256': digest(p['source']), 'system': p['system'], 'file': p['file'],
             'rom_fingerprint': p['rom_fingerprint'], 'matched_rom': matched,
             'provider_game_id': p['provider_game_id'], 'binding': binding(target),
             'fields': copy.deepcopy(fields), 'generator': copy.deepcopy(generator)}
    validate_draft(value, candidate, source=p['source'], system=p['system'], file=p['file'], fingerprint=p['rom_fingerprint'], matched=matched)
    return value


def reviewed_metadata(review, candidate, *, source, system, file, fingerprint, matched, key_path=None, target=None):
    payload = i.verify_payload(review, 'translation_review', review_key(key_path))
    if set(payload) != {'draft', 'status', 'reviewer', 'reviewed_at', 'attestation'} or payload['status'] != 'approved' or payload['attestation'] != 'faithful_translation_no_added_facts' or not isinstance(payload['reviewer'], str) or not payload['reviewer'].strip():
        i._fail('Translation lacks independent approval', 'translation_unreviewed')
    try:
        if datetime.fromisoformat(payload['reviewed_at']).tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError):
        i._fail('Review timestamp is invalid', 'translation_unreviewed')
    result = validate_draft(payload['draft'], candidate, source=source, system=system, file=file, fingerprint=fingerprint, matched=matched)
    if target is not None and binding(target) != payload['draft']['binding']:
        i._fail('Translation approval belongs to another device/root', 'translation_target_mismatch')
    return result


def approve(value, base_receipt, *, reviewer, confirmation, reviewer_key, key_path=None):
    """Explicit independent review boundary; confirmation commits exact bytes."""
    if not isinstance(reviewer, str) or not reviewer.strip() or confirmation != digest(value):
        i._fail('Reviewer must confirm the displayed complete draft digest', 'translation_unreviewed')
    p = i.verify_payload(base_receipt, 'game_identity', key_path)
    i.verify_receipt(base_receipt, system=p['system'], file=p['file'], rom_fingerprint=p['rom_fingerprint'], metadata=p['metadata'], media=p['media'], key_path=key_path)
    _, candidate, matched = i._matched_candidate(p['source'], p['system'], p['rom_fingerprint'], key_path)
    validate_draft(value, candidate, source=p['source'], system=p['system'], file=p['file'], fingerprint=p['rom_fingerprint'], matched=matched)
    review_key(key_path, reviewer_key)
    return i.seal_payload({'draft': value, 'status': 'approved', 'reviewer': reviewer,
                           'reviewed_at': datetime.now(timezone.utc).isoformat(),
                           'attestation': 'faithful_translation_no_added_facts'}, 'translation_review', reviewer_key)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit offline translation drafting and independent review')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-review-key', help='Provision a distinct protected reviewer key; does not approve content')
    init.add_argument('--reviewer-key', required=True)
    init.add_argument('--identity-key')
    for name in ('draft', 'review', 'authorize'):
        p = sub.add_parser(name)
        p.add_argument('--base-catalog', required=True)
        p.add_argument('--identity-key')
        p.add_argument('--out', required=True)
        if name == 'draft':
            p.add_argument('--input', required=True, help='JSON containing fields, generator, binding')
        else:
            p.add_argument('--draft' if name == 'review' else '--review', required=True)
        if name == 'review':
            p.add_argument('--reviewer', required=True)
            p.add_argument('--reviewer-key', required=True, help='Previously provisioned separate protected key')
        if name == 'authorize':
            p.add_argument('--rom', required=True)
            p.add_argument('--media', help='Remeasured actual media files when the base receipt contains media')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init-review-key':
            i._key(args.reviewer_key, create=True)
            review_key(args.identity_key, args.reviewer_key)
            return 0
        catalog = i._load_json(args.base_catalog)
        entries = i.build_catalog([e['receipt'] for e in catalog['entries']], args.identity_key)['entries']
        if len(entries) != 1:
            i._fail('Select one verified game per review', 'invalid_translation')
        base = entries[0]['receipt']
        if args.command == 'draft':
            value = i._load_json(args.input)
            result = draft(base, value['fields'], value['generator'], value['binding'], args.identity_key)
        elif args.command == 'review':
            value = i._load_json(args.draft)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            print('Verify every original and translation; no added facts. Type draft SHA256 to approve: ' + digest(value))
            if not sys.stdin.isatty():
                i._fail('Independent review requires an interactive terminal', 'translation_unreviewed')
            result = approve(value, base, reviewer=args.reviewer, confirmation=input().strip(), reviewer_key=args.reviewer_key, key_path=args.identity_key)
        else:
            p = base['payload']
            review = i._load_json(args.review)
            _, candidate, matched = i._matched_candidate(p['source'], p['system'], p['rom_fingerprint'], args.identity_key)
            translated = reviewed_metadata(review, candidate, source=p['source'], system=p['system'], file=p['file'], fingerprint=p['rom_fingerprint'], matched=matched, key_path=args.identity_key)
            if p['media'] and not args.media:
                i._fail('Supply actual media files again; do not reuse digest claims', 'media_unmeasured')
            receipt = i.authorize_patch(p['source'], system=p['system'], file=p['file'], rom_path=args.rom, metadata={**p['metadata'], **translated}, media=i.load_exact_media(args.media), translation_review=review, key_path=args.identity_key)
            result = i.build_catalog([receipt], args.identity_key)
        i.write_catalog(args.out, result)
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({'status': 'pending_translation', 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
