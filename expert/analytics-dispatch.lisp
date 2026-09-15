(in-package #:llm-log-expert)

(defparameter *analytics-base-dispatch-expert-request*
  (symbol-function 'dispatch-expert-request))

(defun %analytics-payload-string (payload field)
  (let ((value (jsown:val-safe payload field)))
    (and (stringp value) (plusp (length value)) value)))

(defun %dispatch-analytics-query (host operation payload)
  (let ((start (%analytics-payload-string payload "start"))
        (end (%analytics-payload-string payload "end"))
        (provider (%analytics-payload-string payload "provider"))
        (model (%analytics-payload-string payload "model")))
    (%reply-ok
     (cond
       ((equal operation "query_analytics_summary")
        (query-analytics-summary host :start start :end end
                                      :provider provider :model model))
       ((equal operation "query_analytics_models")
        (query-analytics-models host :start start :end end
                                     :provider provider :model model))
       ((equal operation "query_analytics_timeline")
        (let* ((raw (or (%analytics-payload-string payload "granularity") "minute"))
               (granularity
                 (cond ((equal raw "minute") :minute)
                       ((equal raw "hour") :hour)
                       ((equal raw "day") :day)
                       (t (error "invalid analytics granularity"))))
               (coverage (equal (jsown:val-safe payload "coverage") "fields")))
          (query-analytics-timeline
           host :granularity granularity :start start :end end
           :provider provider :model model :coverage coverage)))
       (t (error "unknown analytics operation"))))))

(defun dispatch-expert-request (host request)
  "Extend the typed expert protocol with durable analytics queries."
  (let ((operation (and (consp request) (eq (first request) :obj)
                        (jsown:val-safe request "operation"))))
    (if (member operation
                '("query_analytics_summary" "query_analytics_models"
                  "query_analytics_timeline")
                :test #'equal)
        (handler-case
            (%dispatch-analytics-query host operation (%request-payload request))
          (error (condition)
            (%reply-error "analytics_query_error" (princ-to-string condition))))
        (funcall *analytics-base-dispatch-expert-request* host request))))
