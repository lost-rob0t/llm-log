(in-package #:llm-log-expert)

(defparameter +corpus-checkpoint-schema-version+ 1)
(defparameter +corpus-prefix-fingerprint-bytes+ (* 1024 1024))
(defparameter +default-checkpoint-every+ 1000)

(defun %octets-hex (octets)
  (with-output-to-string (out)
    (loop for byte across octets do (format out "~2,'0x" byte))))

(defun %source-prefix-sha256 (path)
  "Hash only the first MiB so a 50GB source can be identified cheaply."
  (with-open-file (stream path :direction :input :element-type '(unsigned-byte 8))
    (let* ((size (min +corpus-prefix-fingerprint-bytes+ (file-length stream)))
           (buffer (make-array size :element-type '(unsigned-byte 8))))
      (read-sequence buffer stream)
      (%octets-hex (ironclad:digest-sequence :sha256 buffer)))))

(defun %default-corpus-checkpoint (source)
  (pathname (concatenate 'string (uiop:native-namestring source)
                         ".expert-offset.json")))

(defun %read-jsonl-octets (stream)
  "Read one JSONL record from a binary stream.
Returns VALUES bytes start-offset end-offset; NIL at clean EOF."
  (let ((start (file-position stream))
        (buffer (make-array 4096 :element-type '(unsigned-byte 8)
                            :fill-pointer 0 :adjustable t)))
    (loop for byte = (read-byte stream nil nil)
          do (cond
               ((null byte)
                (return
                  (if (zerop (length buffer))
                      (values nil start start)
                      (values buffer start (file-position stream)))))
               ((= byte 10)
                (when (and (plusp (length buffer))
                           (= (aref buffer (1- (length buffer))) 13))
                  (decf (fill-pointer buffer)))
                (return (values buffer start (file-position stream))))
               (t (vector-push-extend byte buffer))))))

(defun %parse-jsonl-octets (bytes source offset)
  (handler-case
      (jsown:parse (trivial-utf-8:utf-8-bytes-to-string bytes))
    (error (condition)
      (error "invalid JSONL at ~A byte ~D: ~A"
             (uiop:native-namestring source) offset condition))))

(defun %checkpoint-json (source fingerprint offset event-id record-transport-evidence)
  (%json-object
   (cons "schema_version" +corpus-checkpoint-schema-version+)
   (cons "source" (uiop:native-namestring (truename source)))
   (cons "source_prefix_sha256" fingerprint)
   (cons "offset" offset)
   (cons "event_id" event-id)
   (cons "record_transport_evidence" (not (null record-transport-evidence)))))

(defun %write-corpus-checkpoint (path source fingerprint offset event-id
                                 record-transport-evidence)
  (ensure-directories-exist path)
  (let ((temporary (pathname (concatenate 'string
                                          (uiop:native-namestring path)
                                          ".tmp"))))
    (with-open-file (stream temporary :direction :output :if-exists :supersede
                                      :if-does-not-exist :create
                                      :external-format :utf-8)
      (write-line
       (jsown:to-json
        (%checkpoint-json source fingerprint offset event-id
                          record-transport-evidence))
       stream)
      (finish-output stream))
    (uiop:rename-file-overwriting-target temporary path)))

(defun %read-corpus-checkpoint (path source fingerprint record-transport-evidence)
  (unless (uiop:file-exists-p path)
    (return-from %read-corpus-checkpoint nil))
  (let* ((state (jsown:parse (uiop:read-file-string path)))
         (version (jsown:val-safe state "schema_version"))
         (stored-source (jsown:val-safe state "source"))
         (stored-fingerprint (jsown:val-safe state "source_prefix_sha256"))
         (stored-mode (not (null (jsown:val-safe state "record_transport_evidence"))))
         (offset (jsown:val-safe state "offset")))
    (unless (eql version +corpus-checkpoint-schema-version+)
      (error "unsupported corpus checkpoint schema in ~A" path))
    (unless (equal stored-source (uiop:native-namestring (truename source)))
      (error "checkpoint ~A belongs to a different source" path))
    (unless (equal stored-fingerprint fingerprint)
      (error "source prefix changed since checkpoint ~A; refusing unsafe resume" path))
    (unless (eql stored-mode (not (null record-transport-evidence)))
      (error "checkpoint ~A used different transport-evidence semantics" path))
    (unless (and (integerp offset) (>= offset 0))
      (error "checkpoint ~A has an invalid byte offset" path))
    offset))

(defun import-capture-corpus
    (host source &key checkpoint from-start require-checkpoint
                       record-transport-evidence (checkpoint-every +default-checkpoint-every+)
                       (limit 0) dry-run)
  "Stream SOURCE exactly once and project successful records directly into HOST.

This is the local high-volume path: no HTTP, no subprocess protocol, and no
whole-corpus materialization. Checkpoints are byte offsets, so resume uses a
single FILE-POSITION seek. A crash can replay at most CHECKPOINT-EVERY already
committed records; stable projection IDs make that replay idempotent."
  (let* ((source (truename source))
         (checkpoint (or checkpoint (%default-corpus-checkpoint source)))
         (fingerprint (%source-prefix-sha256 source))
         (existing-offset (and (not from-start)
                               (%read-corpus-checkpoint
                                checkpoint source fingerprint
                                record-transport-evidence)))
         (offset (or existing-offset 0))
         (seen 0)
         (replayed 0)
         (classified 0)
         (usage-projected 0)
         (transport-outcomes 0)
         (last-event-id nil)
         (last-good-offset offset)
         (since-checkpoint 0))
    (when (and require-checkpoint (null existing-offset))
      (error "local infill requires an existing bulk-load checkpoint: ~A"
             checkpoint))
    (unless (and (integerp checkpoint-every) (plusp checkpoint-every))
      (error "checkpoint-every must be a positive integer"))
    (unless (and (integerp limit) (>= limit 0))
      (error "limit must be a non-negative integer"))
    (with-open-file (stream source :direction :input :element-type '(unsigned-byte 8))
      (when (> offset (file-length stream))
        (error "checkpoint offset ~D exceeds current source size ~D"
               offset (file-length stream)))
      (file-position stream offset)
      (loop
        (when (and (plusp limit) (>= replayed limit)) (return))
        (multiple-value-bind (bytes start end) (%read-jsonl-octets stream)
          (unless bytes (return))
          (when (zerop (length bytes))
            (setf last-good-offset end)
            (loop-finish))
          (incf seen)
          (let* ((event (%parse-jsonl-octets bytes source start))
                 (event-id (%capture-required-string event "event_id")))
            (unless dry-run
              (let ((result (ingest-capture-event
                             host event
                             :record-transport-evidence record-transport-evidence)))
                (incf classified (if (jsown:val-safe result "classified") 1 0))
                (incf usage-projected (if (jsown:val-safe result "usage_projected") 1 0))
                (incf transport-outcomes
                      (if (jsown:val-safe result "transport_outcome") 1 0))))
            (incf replayed)
            (setf last-event-id event-id
                  last-good-offset end)
            (incf since-checkpoint)
            (when (and (not dry-run)
                       (>= since-checkpoint checkpoint-every))
              (%write-corpus-checkpoint checkpoint source fingerprint
                                        last-good-offset last-event-id
                                        record-transport-evidence)
              (setf since-checkpoint 0))))))
    (when (and (not dry-run) last-event-id)
      (%write-corpus-checkpoint checkpoint source fingerprint
                                last-good-offset last-event-id
                                record-transport-evidence))
    (%json-object
     (cons "source" (uiop:native-namestring source))
     (cons "checkpoint" (uiop:native-namestring checkpoint))
     (cons "start_offset" offset)
     (cons "end_offset" last-good-offset)
     (cons "seen" seen)
     (cons "replayed" replayed)
     (cons "classified" classified)
     (cons "usage_projected" usage-projected)
     (cons "transport_outcomes" transport-outcomes)
     (cons "dry_run" (not (null dry-run)))
     (cons "kb_revision" (current-kb-revision host)))))
