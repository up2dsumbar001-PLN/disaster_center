import test from 'node:test';
import assert from 'node:assert/strict';
import { activeWarnings, exposure, validateAssets, parseCap } from '../lib/disaster-model.ts';
import { DOMParser } from '@xmldom/xmldom';
globalThis.DOMParser = DOMParser;

const at = Date.parse('2026-10-28T04:00:00Z');
const warning = (overrides = {}) => ({ id:'a', status:'Actual', msgType:'Alert', references:[], sent:'2026-10-28T03:00:00Z', effective:'2026-10-28T03:00:00Z', expires:'2026-10-28T05:00:00Z', areas:[{ type:'Feature', properties:{}, geometry:{ type:'Polygon', coordinates:[[[100,-1],[101,-1],[101,0],[100,0],[100,-1]]] } }], ...overrides });
const point = (coordinates) => ({ type:'Feature', properties:{name:'gardu'}, geometry:{type:'Point', coordinates} });
test('Only active Actual alerts survive expiry, future and test filtering', () => {
  const list = [warning(),warning({id:'expired',expires:'2026-10-28T04:00:00Z'}),warning({id:'future',effective:'2026-10-28T04:01:00Z'}),warning({id:'test',status:'Test'}),warning({id:'ack',msgType:'Ack'})];
  assert.deepEqual(activeWarnings(list,at).map(w=>w.id),['a']);
});
test('Cancel and Update suppress referenced alerts, including before replacement effective time', () => {
  assert.deepEqual(activeWarnings([warning(),warning({id:'cancel',msgType:'Cancel',references:['a']})],at),[]);
  assert.deepEqual(activeWarnings([warning(),warning({id:'update',msgType:'Update',references:['a']})],at).map(w=>w.id),['update']);
  assert.deepEqual(activeWarnings([warning(),warning({id:'update',msgType:'Update',references:['a'],effective:'2026-10-28T04:01:00Z'})],at),[]);
  assert.deepEqual(activeWarnings([warning(),warning({id:'test-cancel',status:'Test',msgType:'Cancel',references:['a']})],at).map(w=>w.id),['a']);
});
test('CAP intersection distinguishes asset inside, outside, and boundary', () => {
  const rows=exposure([point([100.5,-.5]),point([102,-.5]),point([100,-.5])],[warning()],0);
  assert.deepEqual(rows.map(r=>r.warnings.length),[1,0,1]);
});
test('A crossing feeder is detected even when both endpoints are outside', () => {
  const asset={type:'Feature',properties:{},geometry:{type:'LineString',coordinates:[[99,-.5],[102,-.5]]}};
  assert.equal(exposure([asset],[warning()],0)[0].warnings.length,1);
});
test('Corridor catches adjacent line while zero corridor does not', () => {
  const line={type:'Feature',properties:{},geometry:{type:'LineString',coordinates:[[99.9995,-.8],[99.9995,-.2]]}};
  assert.equal(exposure([line],[warning()],0)[0].warnings.length,0);
  assert.equal(exposure([line],[warning()],100)[0].warnings.length,1);
});
test('Polygon holes do not produce false point exposure', () => {
  const w=warning(); w.areas[0].geometry.coordinates.push([[100.4,-.6],[100.6,-.6],[100.6,-.4],[100.4,-.4],[100.4,-.6]]);
  assert.equal(exposure([point([100.5,-.5])],[w],0)[0].warnings.length,0);
});
test('Asset ingestion rejects invalid coordinates and incomplete geometry', () => {
  const fc=(features)=>({type:'FeatureCollection',features});
  assert.throws(()=>validateAssets(fc([point([-0.9,100.3])])),/Koordinat/);
  assert.throws(()=>validateAssets(fc([{type:'Feature',properties:{},geometry:{type:'LineString',coordinates:[[100,-1]]}}])),/dua/);
  assert.throws(()=>validateAssets(fc([{type:'Feature',properties:{},geometry:{type:'Polygon',coordinates:[[[100,-1],[101,-1],[101,0]]]}}])),/tertutup/);
  assert.equal(validateAssets(fc([point([100.3,-.9])]))[0].properties._dccId,'asset-0');
});
test('CAP namespace and Indonesian language selection preserve WGS84 order and multiple areas', () => {
  const cap=`<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>cap-1</identifier><sent>2026-10-28T03:00:00Z</sent><status>Actual</status><msgType>Alert</msgType><info><language>en-US</language><headline>English</headline></info><info><language>id-ID</language><headline>Hujan Lebat</headline><effective>2026-10-28T03:00:00Z</effective><expires>2026-10-28T05:00:00Z</expires><area><areaDesc>Area pertama</areaDesc><polygon>-1,100 -1,101 0,101 0,100 -1,100</polygon></area><area><areaDesc>Area kedua</areaDesc><circle>-0.9,100.3 5</circle></area></info></alert>`;
  const parsed=parseCap(cap); assert.equal(parsed.title,'Hujan Lebat'); assert.equal(parsed.areas.length,2); assert.deepEqual(parsed.areas[0].geometry.coordinates[0][0],[100,-1]);
  assert.equal(exposure([point([100.3,-.9])],[parsed],0)[0].warnings.length,1);
});
test('Cancel CAP without info can still suppress a previous alert',()=>{
  const c=parseCap('<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>cancel</identifier><sent>2026-10-28T03:30:00Z</sent><status>Actual</status><msgType>Cancel</msgType><references>sender,a,2026-10-28T03:00:00Z</references></alert>');
  assert.deepEqual(activeWarnings([warning(),c],at),[]);
});
