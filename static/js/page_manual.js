(function () {
  // Blueprint 전환으로 endpoint 는 'routes_core.dashboard' 꼴 — manuals 키는
  // 짧은 이름을 유지하므로 마지막 조각으로 정규화한다.
  const endpoint = (document.body.dataset.endpoint || '').split('.').pop();
  const manuals = {
    dashboard: {
      title: '대시보드 설명서',
      sections: [
        ['화면', ['선박별 주요 업무 현황과 자동화 상태를 한 화면에서 확인합니다.', '카드를 누르면 관련 탭이나 상세 화면으로 이동합니다.']]
      ]
    },
    index: {
      title: 'Daily 업무관리 설명서',
      sections: [
        ['주요 버튼', ['신규 이슈: 선박 업무를 새로 등록합니다.', '수정: 선택한 이슈의 내용, 담당자, 상태, 기한을 변경합니다.', '첨부: 관련 파일을 이슈에 붙입니다.', '요약 생성: 현재 업무 목록 기준으로 보고용 요약을 만듭니다.']]
      ]
    },
    automation_page: {
      title: '자동화 모음 설명서',
      sections: [
        ['기본 동작', ['버튼을 누르면 맥미니가 해당 자동화를 실행하고 텔레그램으로 결과를 보고합니다.', '데쿠를 호출하지 않는 0 LLM 실행이며, 러너 폴링 때문에 최대 약 1분 지연될 수 있습니다.', '검증은 읽기전용 미리보기이고 승인, 상신, 실기입을 하지 않습니다.', '자동승인/자동상신은 스케줄러와 동일하게 clean 건만 처리하고 예외는 남깁니다.', '정기 스케줄러는 버튼 실행과 별개로 그대로 유지됩니다.']],
        ['마스터 스위치', ['작동중이면 자동화 버튼이 실행됩니다.', '정지됨이면 버튼 실행이 막히며 정기 스케줄러도 안전정지 상태로 둡니다.']],
        ['SOA 정기검토', ['검증 전체: 4그룹 SOA를 읽기전용으로 점검합니다.', '자동승인 전체: clean 건만 자동 승인하고 예외는 남깁니다.', '리젝체크 전체: 금액 불일치 라인에 리젝 체크만 답니다.', '리젝제출+메일 전체: 리젝 마킹된 SOA를 제출하고 관리사 통보 메일을 보냅니다.']],
        ['선박별 SOA 검증', ['SVMS 등록 전선박 기준 4자 선박코드를 입력한 뒤 실행합니다.', '기간과 부서를 비우면 202601~202612, Technical 기준으로 실행합니다.', '검증 실기입을 누르면 해당 선박 SOA 라인에 CFM_YN 체크와 금액불일치 RJT_YN 리마크를 기입합니다.', '승인, 출금, 리젝제출, 관리사 메일 발송은 하지 않습니다.', 'GPT 5.4 mini 선택 시 과플래그 가능성이 있어 결과 추가확인을 전제로 봅니다.', '결과는 실행 로그와 텔레그램 보고로 확인합니다.']],
        ['전자결재', ['검증: 현재 상신대기 문서를 전수 점검해 보류 후보를 표시합니다.', '자동상신: 보류 제외 후 통과 건만 상신합니다.', '실행 결과는 실행 로그와 텔레그램 보고로 확인합니다.']]
      ]
    },
    health_page: {
      title: '자동화 상태 설명서',
      sections: [['화면', ['각 자동화 러너, 스케줄러, 최근 실행 상태를 확인합니다.', '오래 멈춘 항목이나 실패 로그가 있는 항목을 우선 점검합니다.']]]
    },
    aor_page: {
      title: 'AOR 설명서',
      sections: [['주요 버튼', ['검토: AOR 후보의 금액, 제목, 참조 정보를 확인합니다.', '상신 승인: 확인된 카드만 SVMS 상신 흐름으로 보냅니다.', '보류 또는 제외: 사람이 다시 봐야 하는 건을 자동상신 대상에서 뺍니다.']]]
    },
    fundreq_page: {
      title: '비용청구 설명서',
      sections: [['주요 버튼', ['검토: DN과 Cost Slip 금액, 통화, 참조번호를 대조합니다.', '자동상신: 일치한 clean 건만 상신합니다.', '보류: 불일치나 근거 부족 건은 사람 확인 대상으로 남깁니다.']]]
    },
    invoice_page: {
      title: '인보이스 설명서',
      sections: [['주요 버튼', ['일괄승인+컨펌: 체크된 PASS 인보이스만 SVMS confirm 처리합니다.', 'HOLD 항목은 사유를 확인하고 수동 처리합니다.', '필터는 PASS/HOLD/선박/공급업체 기준으로 목록을 좁힙니다.']]]
    },
    liscr_page: {
      title: '인보이스 등록 설명서',
      sections: [['주요 버튼', ['등록 유형: 기국(LISCR)을 고르면 Vendor·Expense·통화가 고정되고, 기타 인보이스를 고르면 그 값들을 직접 지정합니다.', 'PDF 업로드: 인보이스 PDF를 올리면 맥 러너가 내용을 읽어 카드로 띄웁니다.', '승인 · SVMS 생성: 화면 값을 확인하고 누르면 그때 SVMS에 신규 인보이스가 만들어집니다. 승인 전에는 아무것도 생성되지 않습니다.', 'FIX 카드: 러너가 못 읽은 값이 있다는 뜻입니다. 빈칸을 채우면 승인할 수 있습니다. HOLD는 채워도 승인되지 않습니다.', '취소/삭제: 생성 전 건을 접거나 목록에서 지웁니다.']]]
    },
    class_status_page: {
      title: 'Class Status 설명서',
      sections: [['주요 버튼', ['동기화/업로드: 선급 status 자료를 가져와 선박별 항목으로 반영합니다.', '상세 보기: survey, certificate, due date 정보를 확인합니다.', '첨부: 근거 파일을 항목에 연결합니다.']]]
    },
    condition_survey: {
      title: 'Condition Survey 설명서',
      sections: [['주요 버튼', ['신규 수검: 선박과 분기 기준으로 수검 기록을 만듭니다.', 'Report 추출: 첨부 보고서에서 finding을 자동 추출합니다.', 'Finding 추가/수정: 관찰사항과 조치상태를 관리합니다.']]]
    },
    vetting_status: {
      title: 'Vetting Status 설명서',
      sections: [['주요 버튼', ['신규 Vetting: 검사 기록을 등록합니다.', 'Report 추출: SIRE/검사 보고서에서 OBS와 finding을 뽑습니다.', 'OBS 요약: 선박별 검사 핵심 내용을 요약합니다.']]]
    },
    dry_dock_page: {
      title: 'Dry Dock Report 설명서',
      sections: [['주요 버튼', ['신규 보고서: 입거 보고서 기본 정보를 만듭니다.', '편집: 섹션, 사진, 본문 블록을 작성합니다.', 'Export: 작성한 보고서를 DOCX 또는 PDF로 내보냅니다.']]]
    },
    boarding_page: {
      title: 'Boarding Report 설명서',
      sections: [['주요 버튼', ['신규 보고서: 방선 보고서 기본 정보를 만듭니다.', '편집: 점검 항목, 사진, 코멘트를 작성합니다.', 'Export: 작성한 보고서를 DOCX 또는 PDF로 내보냅니다.']]]
    },
    reqgen_page: {
      title: '도크 자재/수리 설명서',
      sections: [['주요 버튼', ['대기 카드 선택: SVMS에 만들 requisition 후보를 고릅니다.', '템플릿 보기: 입력 형식과 필수값을 확인합니다.', '생성/저장: 선택한 라인을 기준으로 요청서를 만듭니다.']]]
    },
    dock_procure_page: {
      title: 'Dock 발주현황 설명서',
      sections: [['주요 버튼', ['INDEX 엑셀 업로드: R/S/ST 라인을 큐에 적재합니다.', '직접 추가: 메일 견적이나 수동 항목을 추가합니다.', '상태 변경: 발주, 견적, 완료 상태를 갱신합니다.']]]
    },
    krcon_page: {
      title: '룰 검색 설명서',
      sections: [['주요 버튼', ['검색: KRCON 문서에서 키워드와 관련 조항을 찾습니다.', '근거 요약: 검색된 조항을 읽고 요지를 정리합니다.', '문서 보기: 원문 조항을 확인합니다.']]]
    },
    calendar_page: {
      title: 'Calendar 설명서',
      sections: [['주요 버튼', ['새 일정: 선박/업무 일정을 등록합니다.', '편집: 날짜, 제목, 담당, 메모를 수정합니다.', '필터: 선박이나 일정 종류별로 달력을 좁힙니다.']]]
    },
    expenses_page: {
      title: '출장 경비 설명서',
      sections: [['주요 버튼', ['신규 출장: 출장 건을 생성합니다.', '영수증 업로드: 사진에서 금액과 날짜를 추출합니다.', '상세: 출장별 영수증과 정산 내역을 확인합니다.']]]
    },
    expense_detail_page: {
      title: '출장 상세 설명서',
      sections: [['주요 버튼', ['사진 추가: 영수증 이미지를 올립니다.', '자동 추출: 영수증 정보를 표에 추가합니다.', '수정/삭제: 개별 영수증 라인을 정리합니다.']]]
    }
  };
  const manual = manuals[endpoint] || { title: 'TRMT 설명서', sections: [['현재 탭', ['현재 화면의 버튼과 입력값을 확인한 뒤 필요한 작업을 실행합니다.']]] };
  const modal = document.getElementById('page-manual-modal');
  const body = document.getElementById('page-manual-body');
  const title = document.getElementById('page-manual-title');
  const openers = [document.getElementById('btn-page-manual'), document.getElementById('btn-page-manual-mobile')].filter(Boolean);
  const close = document.getElementById('page-manual-close');
  function render() {
    title.textContent = manual.title;
    body.innerHTML = manual.sections.map(([h, items]) =>
      '<section class="page-manual-section"><h3>' + h + '</h3><ul>' +
      items.map(v => '<li>' + v + '</li>').join('') + '</ul></section>'
    ).join('');
  }
  function show() {
    render();
    const menu = document.getElementById('mobile-menu');
    const burger = document.getElementById('nav-burger');
    if (menu && !menu.hidden) {
      menu.hidden = true;
      document.body.classList.remove('mm-lock');
      if (burger) {
        burger.classList.remove('is-open');
        burger.setAttribute('aria-expanded', 'false');
      }
    }
    modal.hidden = false;
    document.body.classList.add('modal-open');
  }
  function hide() {
    modal.hidden = true;
    document.body.classList.remove('modal-open');
  }
  openers.forEach(btn => btn.addEventListener('click', show));
  close.addEventListener('click', hide);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !modal.hidden) hide();
  });
})();
