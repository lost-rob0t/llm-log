(in-package #:llm-log)

(defun %query-params (uri)
  (let ((question (position #\? uri))
        (result nil))
    (when question
      (dolist (part (uiop:split-string (subseq uri (1+ question)) :separator '(#\&)))
        (let ((equals (position #\= part)))
          (push (cons (quri:url-decode (if equals (subseq part 0 equals) part))
                      (quri:url-decode (if equals (subseq part (1+ equals)) "")))
                result))))
    result))

(defun %query-value (params key)
  (cdr (assoc key params :test #'equal)))

(defun %analytics-filter-time (value)
  (when (and value (>= (length value) 16))
    (format nil "~A:00Z" (subseq value 0 16))))

(defun %api-json-response (status object &optional extra-headers)
  (list status
        (append (list :content-type "application/json; charset=utf-8"
                      :cache-control "no-store"
                      :connection "close")
                extra-headers)
        (list (jsown:to-json object))))

(defun %api-html-response (html)
  (list 200
        (list :content-type "text/html; charset=utf-8" :connection "close")
        (list html)))

(defun %openapi-document ()
  (%jobj
   (cons "openapi" "3.1.0")
   (cons "info" (%jobj (cons "title" "llm-log API")
                        (cons "version" "1.0.0")))
   (cons "paths"
         (%jobj
          (cons "/api/v1/stats/summary"
                (%jobj (cons "get" (%jobj (cons "summary" "Aggregate token I/O totals")))))
          (cons "/api/v1/stats/models"
                (%jobj (cons "get" (%jobj (cons "summary" "Token totals by provider/model")))))
          (cons "/api/v1/stats/timeline"
                (%jobj (cons "get" (%jobj (cons "summary" "Bucketed token I/O timeline")))))
          (cons "/api/v1/quotas"
                (%jobj (cons "get" (%jobj (cons "summary" "Provider-reported quota snapshot")))))))))

(defun %handle-analytics-api (env expert-host)
  (let* ((uri (or (getf env :request-uri) "/"))
         (path (let ((q (position #\? uri))) (if q (subseq uri 0 q) uri)))
         (params (%query-params uri))
         (start (%analytics-filter-time (%query-value params "start")))
         (end (%analytics-filter-time (%query-value params "end")))
         (provider (%query-value params "provider"))
         (model (%query-value params "model")))
    (handler-case
        (cond
          ((equal path "/api/v1/stats/summary")
           (%api-json-response
            200 (llm-log-expert:query-analytics-summary
                 expert-host :start start :end end :provider provider :model model)))
          ((equal path "/api/v1/stats/models")
           (%api-json-response
            200 (llm-log-expert:query-analytics-models
                 expert-host :start start :end end :provider provider :model model)))
          ((equal path "/api/v1/stats/timeline")
           (let* ((granularity-text (or (%query-value params "granularity") "minute"))
                  (granularity
                    (cond ((equal granularity-text "minute") :minute)
                          ((equal granularity-text "hour") :hour)
                          ((equal granularity-text "day") :day)
                          (t (error "granularity must be minute, hour, or day"))))
                  (coverage-text (or (%query-value params "coverage") "basic"))
                  (coverage (cond ((equal coverage-text "basic") nil)
                                  ((equal coverage-text "fields") t)
                                  (t (error "coverage must be basic or fields")))))
             (%api-json-response
              200 (llm-log-expert:query-analytics-timeline
                   expert-host :granularity granularity :start start :end end
                   :provider provider :model model :coverage coverage))))
          ((equal path "/api/v1/quotas")
           (%api-json-response 200 (%quota-snapshot-json)))
          ((equal path "/openapi.json")
           (%api-json-response 200 (%openapi-document)))
          ((equal path "/docs")
           (%api-html-response
            "<!doctype html><html><head><title>llm-log API</title></head><body><h1>llm-log API</h1><p>OpenAPI: <a href='/openapi.json'>/openapi.json</a></p></body></html>"))
          (t nil))
      (error (condition)
        (%api-json-response
         400 (%jobj (cons "status" "error")
                     (cons "error" (princ-to-string condition))))))))

(defun %api-path-p (uri)
  (let ((path (let ((q (position #\? uri))) (if q (subseq uri 0 q) uri))))
    (or (uiop:string-prefix-p "/api/v1/" path)
        (equal path "/openapi.json")
        (equal path "/docs"))))

(defun %make-proxy-app (config expert-host)
  (let ((infill-worker (gethash expert-host *proxy-infill-workers*)))
    (lambda (env)
      (let ((uri (or (getf env :request-uri) "/")))
        (if (%api-path-p uri)
            (%handle-analytics-api env expert-host)
            (%proxy-response-callback env config infill-worker))))))
