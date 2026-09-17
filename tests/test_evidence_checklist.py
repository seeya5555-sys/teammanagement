import hashlib
if not hasattr(hashlib,'scrypt'):
    import werkzeug.security
    werkzeug.security.generate_password_hash=lambda value,*a,**k:'test-hash'

import unittest
import routes_calendar_dock as r

class EvidenceChecklistTest(unittest.TestCase):
    def test_aor_complete_and_missing(self):
        full={'attach_files':'["q.pdf"]','match_conf':99,'email_subj':'Vessel RFQ','proposed_comment':'1. 근거'}
        self.assertTrue(r._evidence_checklist('aor',full,[0])['evidence_complete'])
        self.assertEqual(3,r._evidence_checklist('aor',{},[])['gap_count'])

    def test_aor_outlook_failure_is_missing(self):
        d={'attach_files':'["q.pdf"]','match_conf':99,'email_subj':'Outlook 관련 스레드 미확정','proposed_comment':'x'}
        self.assertEqual(1,r._evidence_checklist('aor',d,[0])['gap_count'])

    def test_fund_aor_requires_reference_but_opex_does_not(self):
        base={'attach_files':'["dn.pdf"]','dn':'USD 10'}
        self.assertTrue(r._evidence_checklist('fundreq',{**base,'tp':'O'},[0])['evidence_complete'])
        self.assertEqual(2,r._evidence_checklist('fundreq',{**base,'tp':'A'},[0])['gap_count'])
        self.assertTrue(r._evidence_checklist('fundreq',{**base,'tp':'A','ref_no':'A1','ref_amt':10},[0])['evidence_complete'])
        self.assertEqual(2,r._evidence_checklist('fundreq',{**base,'tp':'X'},[0])['gap_count'])
        self.assertTrue(r._evidence_checklist('fundreq',{**base,'tp':'A','ref_no':'A1','ref_amt':0},[0])['evidence_complete'])

    def test_invoice_presence_only_not_amount_verdict(self):
        d={'attachments':'["inv.pdf"]','match_src':'inv.pdf','has_pdf':False,
           'inv_no_match':0,'amt_match':0,'date_match':0}
        self.assertTrue(r._evidence_checklist('invoice',d,[0])['evidence_complete'])
        self.assertEqual(3,r._evidence_checklist('invoice',{},[])['gap_count'])

    def test_unknown_and_confidence_boundaries(self):
        unknown=r._evidence_checklist('aor',{'match_conf':80,'email_subj':'ok','proposed_comment':'x'},[])
        self.assertEqual('unknown',unknown['items'][0]['state'])
        base={'attach_files':'[]','email_subj':'mail','proposed_comment':'x'}
        self.assertEqual('missing',r._evidence_checklist('aor',{**base,'match_conf':'79%'},[])['items'][1]['state'])
        self.assertEqual('present',r._evidence_checklist('aor',{**base,'match_conf':'80%'},[])['items'][1]['state'])
        self.assertEqual('missing',r._evidence_checklist('aor',{**base,'match_conf':99,'proposed_comment':'   '},[])['items'][2]['state'])

if __name__=='__main__': unittest.main()
