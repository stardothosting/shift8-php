<?php
declare(strict_types=1);

function check(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

check($argc === 3, 'Usage: php runtime-smoke.php TARGET DISCOVERY_JSON');
check(PHP_VERSION === $argv[1], 'Unexpected PHP version: ' . PHP_VERSION);
$baseline = json_decode(file_get_contents($argv[2]), true, 512, JSON_THROW_ON_ERROR);
$required = $baseline['runtime']['loaded_extensions'];
check(is_array($required) && count($required) > 0, 'Missing module baseline');
foreach ($required as $name) {
    check(is_string($name) && extension_loaded($name), 'Missing installed module: ' . $name);
}

check(json_decode(json_encode(['value' => 42]), true)['value'] === 42, 'JSON round trip');
if (extension_loaded('pdo_sqlite')) {
    $pdo = new PDO('sqlite::memory:');
    check((int) $pdo->query('SELECT 42')->fetchColumn() === 42, 'PDO SQLite query');
}
if (extension_loaded('bcmath')) {
    check(bcmul('12', '13', 0) === '156', 'BCMath');
}
if (extension_loaded('mbstring')) {
    check(mb_strlen("\u{00E9}", 'UTF-8') === 1, 'Multibyte Unicode handling');
}
if (extension_loaded('dom')) {
    check((new DOMDocument())->loadXML('<root><value>42</value></root>'), 'DOM XML parsing');
}
check(password_verify('shift8-smoke', password_hash('shift8-smoke', PASSWORD_DEFAULT)), 'Password hashing');

if (extension_loaded('openssl')) {
    $key = openssl_pkey_new(['private_key_bits' => 2048, 'private_key_type' => OPENSSL_KEYTYPE_RSA]);
    check($key !== false, 'OpenSSL key generation');
    check(openssl_sign('shift8-smoke', $signature, $key, OPENSSL_ALGO_SHA256), 'OpenSSL signing');
    $details = openssl_pkey_get_details($key);
    check($details !== false, 'OpenSSL public key');
    check(openssl_verify('shift8-smoke', $signature, $details['key'], OPENSSL_ALGO_SHA256) === 1, 'OpenSSL verification');
    check(openssl_verify('changed-message', $signature, $details['key'], OPENSSL_ALGO_SHA256) === 0, 'OpenSSL rejects changed data');
}

if (extension_loaded('sodium')) {
    $secret = random_bytes(SODIUM_CRYPTO_SECRETBOX_KEYBYTES);
    $nonce = random_bytes(SODIUM_CRYPTO_SECRETBOX_NONCEBYTES);
    check(sodium_crypto_secretbox_open(sodium_crypto_secretbox('smoke', $nonce, $secret), $nonce, $secret) === 'smoke',
        'Sodium encryption round trip');
}
if (extension_loaded('gd')) {
    $image = imagecreatetruecolor(2, 2);
    check($image !== false, 'GD image allocation');
    ob_start();
    check(imagepng($image), 'GD PNG encoding');
    $png = ob_get_clean();
    check(str_starts_with($png, "\x89PNG"), 'GD PNG output');
    imagedestroy($image);
}

if (extension_loaded('apcu')) {
    check(apcu_store('shift8-smoke', 42) && apcu_fetch('shift8-smoke') === 42, 'APCu (requires apc.enable_cli=1)');
    check(apcu_delete('shift8-smoke'), 'APCu cleanup');
}
if (extension_loaded('igbinary')) {
    check(igbinary_unserialize(igbinary_serialize(['value' => 42])) === ['value' => 42], 'Igbinary round trip');
}
if (extension_loaded('msgpack')) {
    check(msgpack_unpack(msgpack_pack(['value' => 42])) === ['value' => 42], 'MessagePack round trip');
}
if (extension_loaded('memcached')) {
    check((new Memcached())->setOption(Memcached::OPT_CONNECT_TIMEOUT, 100), 'Memcached API (no backend connection)');
}
if (extension_loaded('mcrypt')) {
    check(mcrypt_module_self_test(MCRYPT_RIJNDAEL_128) === true, 'Mcrypt self-test');
}
if (extension_loaded('intl')) {
    check(transliterator_transliterate('Any-Latin; Latin-ASCII', "\u{00E9}") === 'e', 'ICU transliteration');
}

$temporary = tempnam(sys_get_temp_dir(), 'shift8-smoke-');
check($temporary !== false, 'Private temporary file');
try {
    if (extension_loaded('zip')) {
        $zip = new ZipArchive();
        check($zip->open($temporary, ZipArchive::CREATE | ZipArchive::OVERWRITE) === true, 'ZIP create');
        check($zip->addFromString('smoke.txt', 'shift8-smoke') && $zip->close(), 'ZIP write');
        check($zip->open($temporary) === true, 'ZIP reopen');
        check($zip->getFromName('smoke.txt') === 'shift8-smoke', 'ZIP round trip');
        check($zip->close(), 'ZIP close');
    }
    if (extension_loaded('FFI')) {
        check(file_put_contents($temporary,
            "#define FFI_SCOPE \"SHIFT8_SMOKE\"\n#define FFI_LIB \"libc.so.6\"\nint abs(int);\n") !== false, 'FFI input');
        $ffi = FFI::load($temporary);
        check($ffi !== null && $ffi->abs(-42) === 42, 'FFI directive parsing and libc call (requires ffi.enable=1)');
    }
} finally {
    check(unlink($temporary), 'Temporary-file cleanup');
}

echo json_encode(['version' => PHP_VERSION, 'required_modules' => $required,
    'status' => 'passed', 'application_health' => 'not-tested'], JSON_THROW_ON_ERROR) . "\n";
