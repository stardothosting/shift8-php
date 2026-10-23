<?php
// Application-independent FPM health script, run through each real pool by lib/fcgi_probe.py.
header('Content-Type: text/plain');
echo "shift8-ok\n";
echo 'sapi=' . PHP_SAPI . "\n";
echo 'version=' . PHP_VERSION . "\n";
foreach (get_loaded_extensions() as $extension) {
    echo 'ext=' . strtolower($extension) . "\n";
}
echo 'openssl=' . (defined('OPENSSL_VERSION_TEXT') ? OPENSSL_VERSION_TEXT : 'none') . "\n";
