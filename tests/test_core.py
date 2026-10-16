import io
import json
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from portkitchen.core import (KitchenError, _safe_member, create_build,
                              selection, super_arguments, unpack)


@pytest.fixture
def firmware(tmp_path):
    donor = tmp_path / 'donor.zip'
    target = tmp_path / 'target.tar.md5'
    with zipfile.ZipFile(donor, 'w') as z:
        z.writestr('AP/system.img', b'donor-system')
        z.writestr('AP/product.img', b'donor-product')
        z.writestr('firmware.json', json.dumps({'model':'SM-D100', 'android_api':35, 'architecture':'arm64'}))
    with tarfile.open(target, 'w') as t:
        for name, data in {'system.img':b'old-system', 'product.img':b'old-product', 'vendor.img':b'target-vendor', 'boot.img':b'target-boot', 'firmware.json':json.dumps({'model':'SM-T100', 'android_api':35, 'architecture':'arm64'}).encode()}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'donor_model':'SM-D100','target_model':'SM-T100','compatibility_confirmed':True,'donor_partitions':['system', 'product']}))
    return donor, target, profile


def test_build_selects_donor_framework_but_keeps_target_hardware(firmware, tmp_path):
    donor, target, profile = firmware
    result = create_build(donor, target, profile, tmp_path/'output')
    assert result['flashable'] is False
    assert result['parts']['system']['source'] == 'donor'
    assert result['parts']['vendor']['source'] == 'target'
    assert (tmp_path/'output/images/boot.img').read_bytes() == b'target-boot'
    assert (tmp_path/'output/images/product.img').read_bytes() == b'donor-product'
    assert 'sha256' in result['parts']['vendor']


def test_requires_review(firmware, tmp_path):
    donor, target, profile = firmware
    data = json.loads(profile.read_text())
    data['compatibility_confirmed'] = False
    profile.write_text(json.dumps(data))
    with pytest.raises(KitchenError, match='compatibility_confirmed'):
        create_build(donor, target, profile, tmp_path/'output')


def test_incompatible_api_blocked(firmware, tmp_path):
    donor, target, profile = firmware
    with zipfile.ZipFile(donor, 'a') as arc:
        arc.writestr('other.txt', 'unused')
    with zipfile.ZipFile(tmp_path/'bad.zip', 'w') as arc:
        arc.writestr('system.img', 'x')
        arc.writestr('product.img', 'y')
        arc.writestr('firmware.json', json.dumps({'model':'SM-D100', 'android_api':34, 'architecture':'arm64'}))
    with pytest.raises(KitchenError, match='android_api'):
        create_build(tmp_path/'bad.zip', target, profile, tmp_path/'output')


def test_rejects_duplicate_image(tmp_path):
    src=tmp_path/'bad.zip'
    with zipfile.ZipFile(src,'w') as arc:
        arc.writestr('A/system.img','a')
        arc.writestr('B/system.img','b')
    with pytest.raises(KitchenError, match='Duplicate'):
        unpack(src, tmp_path/'out')


def test_archive_traversal_rejected(tmp_path):
    assert not _safe_member('../evil')
    assert not _safe_member('C:\\evil')
    src=tmp_path/'evil.zip'
    with zipfile.ZipFile(src,'w') as arc:
        arc.writestr('../system.img','evil')
    with pytest.raises(KitchenError, match='Unsafe'):
        unpack(src,tmp_path/'out')


def test_missing_vendor_blocked(tmp_path):
    fake = tmp_path/'system.img';fake.write_bytes(b'x')
    with pytest.raises(KitchenError, match='vendor'):
        selection({'system':fake},{'system':fake}, {'donor_partitions':['system']})


def test_super_layout_validates_group_and_capacity(tmp_path):
    img = tmp_path/'system.img'; img.write_bytes(b'x'*8192)
    layout={'device_size':1048576*4,'metadata_slots':1, 'groups':{'main':4096}, 'partitions':{'system':'main'}}
    with patch('portkitchen.core.tool', return_value='lpmake'):
        with pytest.raises(KitchenError, match='too small'):
            super_arguments({'system':img}, layout, tmp_path/'super.img')
        layout['groups']['main']=1048576
        args=super_arguments({'system':img}, layout, tmp_path/'super.img')
    assert '--sparse' in args
    assert 'system:readonly:8192:main' in args


def test_slot_mismatch_rejected(tmp_path):
    file = tmp_path/'x.img';file.write_bytes(b'x')
    with pytest.raises(KitchenError, match='slot'):
        selection({'system_a':file},{'vendor':file,'system':file},{'donor_partitions':['system']})


def test_nested_ap_tar_in_zip(tmp_path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as tar:
        entry=tarfile.TarInfo('system.img')
        entry.size=4
        tar.addfile(entry, io.BytesIO(b'boot'))
    src=tmp_path/'bundle.zip'
    with zipfile.ZipFile(src,'w') as arc:
        arc.writestr('firmware/AP_TEST.tar.md5',payload.getvalue())
    images,_=unpack(src,tmp_path/'out')
    assert images['system'].read_bytes()==b'boot'


def test_direct_image(tmp_path):
    src=tmp_path/'system.img'
    src.write_bytes(b'image')
    images,_=unpack(src,tmp_path/'out')
    assert images['system'].read_bytes()==b'image'


def test_tar_symlink_rejected(tmp_path):
    src=tmp_path/'evil.tar'
    with tarfile.open(src,'w') as arc:
        member=tarfile.TarInfo('system.img')
        member.type=tarfile.SYMTYPE
        member.linkname='../../etc/passwd'
        arc.addfile(member)
    with pytest.raises(KitchenError, match='symlinks'):
        unpack(src,tmp_path/'out')


def test_profile_template_is_never_auto_approved(firmware, tmp_path):
    from portkitchen.core import create_profile_template
    donor, target, _ = firmware
    result = create_profile_template(donor, target, tmp_path / 'review')
    profile = json.loads((tmp_path / 'review/profile-template.json').read_text())
    assert profile['compatibility_confirmed'] is False
    assert profile['donor_model'] == 'SM-D100'
    assert profile['target_model'] == 'SM-T100'
    assert profile['donor_partitions'] == ['system', 'product']
    assert result['flashable'] is False
    assert result['target_partitions']['vendor'] == len(b'target-vendor')
    assert (tmp_path/'review/review.json').exists()


def test_profile_template_refuses_existing_output(firmware, tmp_path):
    from portkitchen.core import create_profile_template
    donor, target, _ = firmware
    (tmp_path/'review').mkdir()
    (tmp_path/'review/previous.txt').write_text('existing')
    with pytest.raises(KitchenError, match='empty'):
        create_profile_template(donor, target, tmp_path / 'review')


def test_profile_command(firmware, tmp_path):
    from portkitchen.__main__ import main
    donor, target, _ = firmware
    assert main(['profile', '--donor', str(donor), '--target', str(target), '--output', str(tmp_path / 'review')]) == 0
    assert (tmp_path/'review/profile-template.json').exists()


def test_copy_is_bounded(tmp_path):
    from portkitchen.core import _copy_limited
    with pytest.raises(KitchenError, match='too large'):
        _copy_limited(io.BytesIO(b'abcdef'), tmp_path/'overflow', 5)


def test_rejects_invalid_manifest_type(tmp_path):
    src = tmp_path / 'bad.zip'
    with zipfile.ZipFile(src, 'w') as arc:
        arc.writestr('system.img', b'images')
        arc.writestr('firmware.json', '[]')
    with pytest.raises(KitchenError, match='JSON object'):
        unpack(src, tmp_path / 'unpack')

def test_rejects_invalid_profile_type(firmware, tmp_path):
    donor, target, profile = firmware
    profile.write_text('[]')
    with pytest.raises(KitchenError, match='JSON object'):
        create_build(donor, target, profile, tmp_path / 'out')
