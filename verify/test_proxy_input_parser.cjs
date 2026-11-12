// Pure parsing checks: synthetic addresses/credentials only, no network requests.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../lan_seats/lib/client.js'),'utf8');
const definition=source.match(/^    function parseProxyInput\(value\) \{[\s\S]*?^    \}/m)?.[0];
assert.ok(definition,'the proxy parser must exist');
const parse=vm.runInNewContext('('+definition+')');
const plain=value=>JSON.parse(JSON.stringify(parse(value)));
assert.deepEqual(plain('192.0.2.10:12324:fixture-user:fixture-password'),{host:'192.0.2.10',port:'12324',username:'fixture-user',password:'fixture-password'});
assert.deepEqual(plain('[192.0.2.10:12324](http://192.0.2.10:12324):fixture-user:fixture-password'),plain('192.0.2.10:12324:fixture-user:fixture-password'));
assert.deepEqual(plain('[2001:db8::1]:8080:fixture:one:two:@/%'),{host:'2001:db8::1',port:'8080',username:'fixture',password:'one:two:@/%'});
assert.deepEqual(plain('proxy.example.test:8080:fixture:尾部空格 '),{host:'proxy.example.test',port:'8080',username:'fixture',password:'尾部空格 '});
assert.deepEqual(plain('\r\n192.0.2.10:8080:fixture:secret\r\n'),plain('192.0.2.10:8080:fixture:secret'));
assert.deepEqual(plain('192.0.2.10:8080'),{host:'192.0.2.10',port:'8080',username:'',password:''});
assert.deepEqual(plain('192.0.2.10:8080::'),plain('192.0.2.10:8080'));
for(const invalid of ['192.0.2.10','192.0.2.10:0:fixture:secret','192.0.2.10:65536:fixture:secret','192.0.2.10:abc:fixture:secret',
  '192.0.2.10:8080:fixture','192.0.2.10:8080:fixture:secret\nsecond-line','http://192.0.2.10:8080:fixture:secret','2001:db8::1:8080:fixture:secret']){
  assert.equal(parse(invalid),null,'incomplete/invalid input must not replace the four fields');
}
console.log('PASS: proxy input splits credentials, IPv6 and copied links; preserves password characters; rejects incomplete/invalid input.');
