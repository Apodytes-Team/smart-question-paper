; Example problem for the tutor.

#!/usr/bin/racket
#lang racket/base
#lang racket

(require "term-parser.rkt")
(require "tutor.rkt")

(tutor
  (list (parse-term "2x = 1 + 2 + 3"))
  (list (solve-for "x")))
