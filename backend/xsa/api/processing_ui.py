"""Shared, human-readable queue and worker wording for both operations views."""

PROCESSING_UI_JS = r"""
function isWaitingDiarizationJob(job) {
  return job.job_type === 'diarize' &&
    ['queued', 'retrying', 'waiting_for_resources', 'waiting_for_configuration'].includes(job.state);
}
function processingJobLabels(job, lang='ar') {
  const ar = lang === 'ar';
  const states = ar ? {
    queued:'في الانتظار', retrying:'بانتظار إعادة المحاولة', running:'قيد التنفيذ',
    failed:'فشلت', completed:'اكتملت', cancelled:'ملغاة', cancelling:'جارٍ الإلغاء',
    waiting_for_resources:'بانتظار توفر الموارد', waiting_for_configuration:'بانتظار إعداد العامل'
  } : {
    queued:'Queued', retrying:'Waiting to retry', running:'Running', failed:'Failed',
    completed:'Completed', cancelled:'Cancelled', cancelling:'Cancelling',
    waiting_for_resources:'Waiting for resources', waiting_for_configuration:'Waiting for configuration'
  };
  const unstarted = !job.started_at && Number(job.attempt || 0) === 0;
  return {
    type:job.job_type==='diarize'?(ar?'فصل المتحدثين':'Speaker diarization'):
      job.job_type==='post_process'?(ar?'التفريغ والتحليل':'Transcription and analysis'):
      job.job_type==='prepare_media'?(ar?'تجهيز الفيديو':'Video preparation'):job.job_type,
    state:job.state==='queued' && job.job_type==='diarize' && !job.worker_id
      ?(ar?'بانتظار عامل الفصل':'Waiting for diarization worker'):(states[job.state]||job.state),
    stage:unstarted?(ar?'لم يبدأ التنفيذ':'Not started'):
      job.stage && job.stage!==job.state?job.stage:(ar?'—':'—'),
    attempt:unstarted?(ar?'لم تبدأ أي محاولة':'No attempts yet'):
      `${ar?'محاولة':'Attempt'} ${Number(job.attempt||0)}`,
    worker:job.worker_id || (unstarted?(ar?'يُعيّن عند بدء التنفيذ':'Assigned when execution starts'):'—')
  };
}
function diarizationWorkerMessage(diar, lang='ar') {
  const ar=lang==='ar', worker=diar?.worker||{};
  if(worker.status==='unavailable' || !worker.status)
    return ar?'لا توجد إشارة حياة حديثة من عامل فصل المتحدثين. المهام محفوظة في قائمة الانتظار.':
      'No recent heartbeat from the diarization worker. Queued jobs are preserved.';
  if(worker.status==='stopped') return ar?'عامل فصل المتحدثين متوقف.':'The diarization worker is stopped.';
  if(worker.reason==='model_access_not_configured')
    return ar?'عامل الفصل متصل، لكن الوصول إلى النموذج غير مُعدّ. يلزم إعداد HF_TOKEN وصلاحية النموذج.':
      'Worker online, but model access is not configured. HF_TOKEN and model access are required.';
  if(worker.reason==='dependencies_missing')
    return ar?'عامل الفصل متصل، لكن مكتبات النموذج غير مكتملة.':'Worker online, but model dependencies are missing.';
  if(worker.phase==='error') return ar?'عامل الفصل أبلغ عن خطأ؛ سيعيد المحاولة.':'Worker reported an error; it will retry.';
  if(Number(diar?.running_jobs)>0) return ar?'توجد مهمة فصل متحدثين قيد التنفيذ.':'A diarization job is running.';
  return ar?'عامل الفصل متصل ويفحص قائمة الانتظار. يُسمح بالتحليل بعد التحقق من خمول التسجيل والتفريغ.':
    'Worker online and polling. Analysis requires verified idle recording and transcription.';
}
"""
