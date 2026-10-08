"""Synthetic offline derivatives exercise existing prepare/deploy/rollback gates."""
import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch
import test_identity as fixtures
import test_identity_deploy as deployment
import identity as i
import translations as t
import esde_core as core
import screenscraper


class TranslationTests(unittest.TestCase):
    def setUp(self):
        fixtures.IdentityTests.setUp(self)
        self.review_key = self.root / 'reviewer' / 'key.json'
        i._key(self.review_key, create=True)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {t.REVIEW_KEY_ENV: str(self.review_key)}).start()
        self.base = i.authorize_patch(self.source, system=self.system, file=self.file, rom_path=self.rom, metadata=self.metadata, key_path=self.key)
        self.target = {'kind': 'local', 'system_rom_root': str(self.root.resolve())}
        self.value = t.draft(self.base, {'name': {'source_field': 'name', 'original': self.metadata['name'], 'text': '审核后的中文名称', 'language': 'zh-CN'}}, {'model': 'synthetic-test', 'prompt_sha256': 'a'*64}, self.target, self.key)
        self.review = self.approve(self.value)
        self.translated = {**self.metadata, 'name': '审核后的中文名称'}

    def approve(self, value):
        return t.approve(value, self.base, reviewer='Independent fixture reviewer', confirmation=t.digest(value), reviewer_key=self.review_key, key_path=self.key)

    def authorize(self, review=None, **kwargs):
        return i.authorize_patch(self.source, system=self.system, file=self.file, rom_path=self.rom, metadata=self.translated, key_path=self.key, translation_review=self.review if review is None else review, **kwargs)

    def verify(self, receipt, **kwargs):
        arguments = dict(system=self.system, file=self.file, rom_fingerprint=self.fp, metadata=self.translated, key_path=self.key, target_binding=self.target)
        arguments.update(kwargs)
        return i.verify_receipt(receipt, **arguments)

    def test_approved_derivative_preserves_original_and_source(self):
        receipt = self.authorize()
        self.verify(receipt)
        self.assertEqual(receipt['payload']['source'], self.source)
        self.assertEqual(receipt['payload']['matched_rom'], self.record)
        self.assertEqual(receipt['payload']['translation_review']['payload']['draft']['fields']['name']['original'], self.metadata['name'])
        i.build_catalog([receipt], self.key)

    def test_tampering_text_original_review_and_source_rejected(self):
        for member in ('text', 'original', 'binding', 'status', 'source'):
            review = copy.deepcopy(self.review)
            draft = review['payload']['draft']
            if member in ('text', 'original'):
                draft['fields']['name'][member] = '篡改内容'
            elif member == 'binding':
                draft['binding']['system_rom_root'] = '/other'
            elif member == 'source':
                draft['source_sha256'] = '0'*64
            else:
                review['payload']['status'] = 'pending'
            with self.subTest(member=member), self.assertRaises(i.IdentityError):
                self.authorize(review)

    def test_unreviewed_draft_boolean_and_wrong_seal_kind_rejected(self):
        for review in (self.value, {'approved': True}, self.source, i.seal_payload(self.review['payload'], 'translation_review', self.key)):
            with self.subTest(review=str(review)[:30]), self.assertRaises(i.IdentityError):
                self.authorize(review)
        with self.assertRaises(i.IdentityError):
            i.authorize_patch(self.source, system=self.system, file=self.file, rom_path=self.rom, metadata=self.translated, key_path=self.key)

    def test_signed_pending_review_and_changed_trusted_original_rejected(self):
        payload = copy.deepcopy(self.review['payload']); payload['status'] = 'pending'
        with self.assertRaises(i.IdentityError):
            self.authorize(i.seal_payload(payload, 'translation_review', self.review_key))
        value = copy.deepcopy(self.value); value['fields']['name']['original'] = 'invented'
        with self.assertRaises(i.IdentityError):
            self.approve(value)

    def test_cli_draft_review_noninteractive_and_authorize(self):
        import contextlib
        import io
        base = self.root / 'base.json'; inputs = self.root / 'input.json'
        pending = self.root / 'draft.json'; reviewed = self.root / 'review.json'
        out = self.root / 'translated.json'
        core.write_json(base, i.build_catalog([self.base], self.key))
        core.write_json(inputs, {key: self.value[key] for key in ('binding', 'generator', 'fields')})
        common = ['--base-catalog', str(base), '--identity-key', str(self.key)]
        self.assertEqual(t.main(['draft', *common, '--input', str(inputs), '--out', str(pending)]), 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), patch('sys.stdin', io.StringIO('')):
            self.assertEqual(t.main(['review', *common, '--draft', str(pending), '--reviewer', 'fixture', '--reviewer-key', str(self.review_key), '--out', str(reviewed)]), 3)
        self.assertFalse(reviewed.exists())
        core.write_json(reviewed, self.review)
        self.assertEqual(t.main(['authorize', *common, '--review', str(reviewed), '--rom', str(self.rom), '--out', str(out)]), 0)
        self.verify(core.load_json(out)['entries'][0]['receipt'])

    def test_wrong_game_platform_version_original_and_added_facts_rejected_at_review(self):
        for member, value in [('provider_game_id', 'other'), ('system', 'gba'), ('matched_rom', {**self.record, 'revision': '3'}), ('source_sha256', '0'*64), ('classification', 'ai_supplement')]:
            draft = copy.deepcopy(self.value); draft[member] = value
            with self.subTest(member=member), self.assertRaises(i.IdentityError):
                self.approve(draft)
        for mutation in ('field', 'original', 'control', 'empty'):
            draft = copy.deepcopy(self.value)
            if mutation == 'field':
                draft['fields']['publisher'] = draft['fields'].pop('name')
            elif mutation == 'original':
                draft['fields']['name']['original'] = 'invented original'
            else:
                draft['fields']['name']['text'] = '\x00中文' if mutation == 'control' else ''
            with self.subTest(mutation=mutation), self.assertRaises(i.IdentityError):
                self.approve(draft)

    def test_review_requires_exact_confirmation_and_separate_key(self):
        for confirmation, key in [('no', self.review_key), (t.digest(self.value), self.key)]:
            with self.assertRaises(i.IdentityError):
                t.approve(self.value, self.base, reviewer='reviewer', confirmation=confirmation, reviewer_key=key, key_path=self.key)

    def test_cross_installation_device_and_root_reuse_rejected(self):
        receipt = self.authorize()
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, target_binding={'kind': 'local', 'system_rom_root': '/other'})
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, key_path=self.root / 'different-key')
        value = copy.deepcopy(self.value)
        value['binding'] = {'kind': 'android', 'serial': 'fixture-A', 'remote_rom_root': '/storage/roms'}
        receipt = self.authorize(self.approve(value))
        self.verify(receipt, target_binding=value['binding'])
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, target_binding={**value['binding'], 'serial': 'fixture-B'})
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, target_binding={**value['binding'], 'remote_rom_root': '/other'})

    def test_changed_rom_and_post_review_write_payload_rejected(self):
        receipt = self.authorize()
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, rom_fingerprint={**self.fp, 'sha256': '0'*64})
        with self.assertRaises(i.IdentityError):
            self.verify(receipt, metadata={**self.translated, 'desc': '未审核补充'})
        self.rom.write_bytes(b'changed fixture')
        with self.assertRaises(i.IdentityError):
            self.authorize()

    def test_prepare_gate_checks_target_and_preserves_history(self):
        receipt = self.authorize()
        catalog = i.build_catalog([receipt], self.key)
        root = self.root / 'roms'; (root / self.file).parent.mkdir(parents=True)
        (root / self.file).write_bytes(self.rom.read_bytes())
        doc = core.parse_document('<gameList><game><path>./'+self.file+'</path><playcount>7</playcount></game></gameList>')
        args = dict(system=self.system, identity_catalog=catalog, identity_key_path=self.key)
        patch_value = [{'file': self.file, 'metadata': self.translated}]
        with self.assertRaises(i.IdentityError):
            core.normalize_document(doc, root, [self.file], patch_value, **args)
        output, _ = core.normalize_document(doc, root, [self.file], patch_value, translation_target=self.target, **args)
        self.assertEqual(output.findtext('gameList/game/playcount'), '7')
        self.assertEqual(output.findtext('gameList/game/name'), self.translated['name'])

    def test_provider_retains_original_synopsis_without_claiming_chinese_fact(self):
        candidate = screenscraper.normalize_game({'nom': 'Test', 'synopsis': [{'langue': 'en', 'text': 'Original description'}]})
        self.assertEqual(candidate['original_description'], 'Original description')
        self.assertIsNone(candidate['description_zh'])


class TranslationDeploymentTests(unittest.TestCase):
    def setUp(self):
        deployment.IdentityDeploymentTests.setUp(self)
        self.review_key = self.root / 'review-key.json'
        i._key(self.review_key, create=True)
        patch.dict(os.environ, {t.REVIEW_KEY_ENV: str(self.review_key)}).start()
        base = self.entry['receipt']; p = base['payload']
        value = t.draft(base, {'name': {'source_field': 'name', 'original': 'Super Mario', 'text': '超级马里奥测试', 'language': 'zh-CN'}}, {'model': 'fixture', 'prompt_sha256': 'a'*64}, {'kind': 'local', 'system_rom_root': str(self.rom.parent.resolve())})
        review = t.approve(value, base, reviewer='fixture-reviewer', confirmation=t.digest(value), reviewer_key=self.review_key)
        receipt = i.authorize_patch(p['source'], system='nds', file='Mario.nds', rom_path=self.rom, metadata={'name': '超级马里奥测试'}, translation_review=review)
        core.write_json(self.catalog, i.build_catalog([receipt]))
        self.source.write_bytes(self.original.replace(b'<name>Mario</name>', '<name>超级马里奥测试</name>'.encode()))

    command = deployment.IdentityDeploymentTests.command
    plan = deployment.IdentityDeploymentTests.plan

    def test_install_and_rollback_restore_exact_original_without_review_key(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command('apply', '--run', str(self.run)), 0)
        self.assertIn('超级马里奥测试'.encode(), self.target_xml.read_bytes())
        self.assertIn(b'<playcount>4</playcount>', self.target_xml.read_bytes())
        self.review_key.unlink()
        self.assertEqual(self.command('rollback', '--run', str(self.run)), 0)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_cli_prepare_freezes_original_translation_and_review(self):
        import contextlib
        import io
        import esde
        patch_file = self.root / 'translation-patch.json'
        catalog = core.load_json(self.catalog)
        metadata = catalog['entries'][0]['receipt']['payload']['metadata']
        core.write_json(patch_file, [{'file': 'Mario.nds', 'metadata': metadata}])
        output = self.root / 'translated-prepared.xml'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = esde.main(['prepare', '--system', 'nds', '--gamelist', str(self.target_xml),
                               '--system-rom-root', str(self.rom.parent), '--patch', str(patch_file),
                               '--identity-catalog', str(self.catalog), '--out', str(output)])
        self.assertEqual(status, 0)
        proof = i.verify_preparation(core.load_json(str(output) + '.identity.json'))
        self.assertEqual(Path(proof['source_path']).read_bytes(), self.original)
        self.assertEqual(proof['entries'][0]['receipt'], catalog['entries'][0]['receipt'])
        self.assertIn('超级马里奥测试'.encode(), output.read_bytes())

    def test_review_key_change_after_plan_blocks_apply_before_write(self):
        self.assertEqual(self.plan(), 0)
        self.review_key.unlink(); i._key(self.review_key, create=True)
        self.assertEqual(self.command('apply', '--run', str(self.run)), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_wrong_target_root_rejects_even_identical_rom(self):
        other = self.root / 'other-roms'; (other / 'nds').mkdir(parents=True)
        (other / 'nds' / 'Mario.nds').write_bytes(self.rom.read_bytes())
        self.rom_root = other
        self.assertEqual(self.plan(), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)
